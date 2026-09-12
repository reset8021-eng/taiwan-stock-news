"""
netutil.py — 共用的連線工具

台灣兩個交易所的 OpenAPI 有不同的 TLS 問題，這裡集中處理：

TWSE  正常，完整驗證即可。
TPEx  伺服器沒有送出中繼憑證（憑證鏈不完整）。
      Windows 會自動上網補抓缺失的中繼憑證（AIA fetching），所以本機能過；
      Linux 預設不做這件事，於是 GitHub Actions 上會失敗。
      另外 Python 3.13+ 的嚴格檢查還會抱怨憑證缺 Subject Key Identifier。

處理順序（fetch_json 的 allow_insecure=True 時）：
    1. 完整驗證
    2. 放寬 RFC 5280 嚴格檢查（保留憑證鏈驗證）
    3. 關閉驗證，但對回傳內容做結構檢查，並印出明顯警告

第 3 層是刻意的取捨：抓的是公開、唯讀、無帳號密碼的資料，
代價是理論上無法察覺中間人竄改。若不接受，把 allow_insecure 設為 False，
櫃買資料就會缺席，程式仍會以上市資料繼續運作。
"""

import ssl
import sys
import warnings

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.ssl_ import create_urllib3_context

try:
    import certifi
    CA_BUNDLE = certifi.where()
except ImportError:
    CA_BUNDLE = None

HEADERS = {"User-Agent": "tw-stock-pipeline/0.2"}


class _Adapter(HTTPAdapter):
    def __init__(self, strict=True, verify=True, **kw):
        self._strict = strict
        self._verify = verify
        super().__init__(**kw)

    def init_poolmanager(self, *args, **kwargs):
        ctx = create_urllib3_context()
        if self._verify:
            if CA_BUNDLE:
                ctx.load_verify_locations(cafile=CA_BUNDLE)
            else:
                ctx.load_default_certs()
            if not self._strict:
                ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
        else:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        kwargs["ssl_context"] = ctx
        return super().init_poolmanager(*args, **kwargs)


def _session(strict=True, verify=True):
    s = requests.Session()
    s.headers.update(HEADERS)
    s.mount("https://", _Adapter(strict=strict, verify=verify))
    return s


def looks_like_openapi_payload(data):
    """結構檢查：必須是非空清單，且每筆是帶中文欄位名的字典。

    關閉憑證驗證時，這是唯一還能擋下明顯異常回應的防線
    （例如被攔截後塞回一個 HTML 錯誤頁或空物件）。
    """
    if not isinstance(data, list) or not data:
        return False
    first = data[0]
    if not isinstance(first, dict) or not first:
        return False
    return any(any("\u4e00" <= ch <= "\u9fff" for ch in k) for k in first)


def fetch_json(url, allow_insecure=False, timeout=30):
    """回傳 (資料, 使用的驗證層級)。全部失敗則拋出最後一個例外。"""
    attempts = [("strict", True, True), ("relaxed", False, True)]
    if allow_insecure:
        attempts.append(("insecure", False, False))

    last_err = None
    for level, strict, verify in attempts:
        try:
            if level == "insecure":
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    r = _session(strict, verify).get(url, timeout=timeout, verify=False)
            else:
                r = _session(strict, verify).get(url, timeout=timeout)
            r.raise_for_status()
            data = r.json()

            if level == "insecure":
                if not looks_like_openapi_payload(data):
                    raise ValueError("關閉驗證後取得的內容不符預期格式，已丟棄")
                print(
                    f"  ⚠ {url}\n"
                    f"    憑證驗證失敗，已降級為不驗證連線取得資料。\n"
                    f"    原因：來源伺服器未提供完整憑證鏈。資料已通過結構檢查。",
                    file=sys.stderr,
                )
            return data, level

        except Exception as e:
            last_err = e
            continue

    raise last_err
