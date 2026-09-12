"""
textnorm.py — 標題與網址的正規化，以及相似度計算

這裡全部是純函式，沒有網路、沒有資料庫，所以可以單獨執行自我測試：

    python textnorm.py

為什麼要獨立成一支檔案：聚合演算法好不好用，九成取決於正規化有沒有把
「同一件事的不同寫法」磨成同一個樣子。這部分需要反覆調整，把它跟抓取、
資料庫寫入分開，改動時不會牽動別的東西。
"""

import hashlib
import re
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# ------------------------------------------------------------------ 標題正規化

# 開頭的來源或體例標記：【XX報導】〈盤中速報〉（獨家）[快訊]
# 只吃開頭且長度上限 20，避免把標題中間正常使用的括號內容吃掉，
# 例如「郭明錤：台積電（先進封裝）產能…」這種。
_LEAD_BRACKET = re.compile(r"^\s*[【〈《（(\[［]([^】〉》）)\]］]{1,20})[】〉》）)\]］]\s*")

# 開頭的旗標詞加分隔符：快訊／　獨家/　盤中速報：
_LEAD_FLAG = re.compile(
    r"^\s*(快訊|獨家|即時|更新|直擊|盤中速報|盤後速報|焦點|頭條|重磅|一文看懂)"
    r"\s*[／/：:\-－—]\s*"
)

# 股號標記。交接文件寫「去尾綴股號」，這裡刻意改成「不分位置一律去掉」。
# 理由：同一則消息，A 媒體寫「台積電（2330）法說會」、B 媒體寫「台積電法說會」，
# 如果只去尾綴，中間的（2330）會讓兩邊的字元 3-gram 差掉 6 個以上，
# 在 0.62 這種門檻下足以把該併的兩則拆開。
# 代價：標題裡出現的四位數字若剛好被括號包住會一起被拿掉（例如「上看(2500)點」），
# 這種寫法極少見，而且拿掉的是雜訊不是區辨特徵，划算。
_STOCK_CODE = re.compile(r"[（(]\s*\d{4}\s*(?:[-.．]\s*(?:TW|TWO))?\s*[)）]", re.IGNORECASE)

# 標點與空白。相似度只看字，不看標點，
# 因為媒體對全形驚嘆號、空格、破折號的用法完全沒有共識。
_PUNCT = re.compile(
    r"[\s\u3000!-/:-@\[-`{-~！？。，、；：「」『』（）〔〕【】〈〉《》…—－·‧～〜｜|]"
)


def normalize_title(title: str) -> str:
    """回傳只用於比對的正規化標題。原標題永遠不覆寫，這個結果不入庫。

    保留數字：目標價、營收數字、奈米製程是重要的區辨特徵，
    把「目標價上看2000元」和「目標價上看1500元」磨成一樣會出大事。
    """
    if not title:
        return ""

    # NFKC 會把全形英數與全形括號轉成半形，順便統一各種相容字元
    s = unicodedata.normalize("NFKC", title)

    # 前綴可能疊兩層，例如「【財訊】〈獨家〉…」，最多剝兩次就好，
    # 再多通常代表這個標題本身就是括號開頭的正文
    for _ in range(2):
        new = _LEAD_BRACKET.sub("", s)
        if new == s:
            break
        s = new
    s = _LEAD_FLAG.sub("", s)

    s = _STOCK_CODE.sub("", s)
    s = _PUNCT.sub("", s)
    return s.lower()


# ------------------------------------------------------------------ 相似度

def char_ngrams(s: str, n: int = 3) -> set:
    """字元 n-gram。中文沒有空白斷詞，字元 n-gram 比斷詞穩定也不需要詞庫。"""
    if len(s) < n:
        return {s} if s else set()
    return {s[i:i + n] for i in range(len(s) - n + 1)}


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if inter == 0:
        return 0.0
    return inter / len(a | b)


def title_similarity(t1: str, t2: str, n: int = 3) -> float:
    """兩個「已正規化」標題的相似度。傳原標題進來會算錯，呼叫端要先正規化。"""
    return jaccard(char_ngrams(t1, n), char_ngrams(t2, n))


# ------------------------------------------------------------------ 網址正規化

# 追蹤參數。媒體會在不同管道掛不同的 utm，同一篇文章因此長出好幾個網址。
_DROP_PARAM_PREFIX = ("utm_",)
_DROP_PARAM_EXACT = {
    "fbclid", "gclid", "yclid", "dclid", "msclkid", "igshid",
    "mc_cid", "mc_eid", "ref", "ref_src", "refsrc", "from", "source",
    "share", "share_from", "spm", "cmpid", "campaign_id", "at_medium",
}


def normalize_url(url: str) -> str:
    """把同一篇文章的各種網址寫法收斂成一個。

    做的事：統一 https、去 www.、去 fragment、去追蹤參數、
    保留其餘參數但排序（有些站台靠 ?id= 決定內容，不能全砍）、
    去 amp 尾段、去結尾斜線。
    """
    if not url:
        return ""
    u = url.strip()
    if u.startswith("//"):
        u = "https:" + u
    if not re.match(r"^https?://", u, re.IGNORECASE):
        u = "https://" + u

    parts = urlsplit(u)
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    host = host.rstrip(".")

    path = parts.path
    # amp 版本與正常版本是同一篇，收斂掉
    path = re.sub(r"(?:/amp|\.amp|/amp\.html)$", "", path, flags=re.IGNORECASE)
    path = re.sub(r"/+$", "", path)

    kept = [
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=False)
        if not k.lower().startswith(_DROP_PARAM_PREFIX)
        and k.lower() not in _DROP_PARAM_EXACT
    ]
    query = urlencode(sorted(kept))

    return urlunsplit(("https", host, path, query, ""))


def clean_url(url: str) -> str:
    """給人點的網址：只拿掉追蹤參數與 fragment，主機名一字不動。

    跟 normalize_url 的差別很重要。normalize_url 會把 www. 拿掉、強制 https、
    砍掉 amp 尾段，那是為了讓同一篇文章的各種寫法算出同一個雜湊；
    但有些站台改了主機名或協定就打不開，所以存進資料庫給人點的必須是這一個。
    一句話：normalize_url 給機器比對，clean_url 給人點。
    """
    if not url:
        return ""
    parts = urlsplit(url.strip())
    if not parts.scheme:
        return url.strip()
    kept = [
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=False)
        if not k.lower().startswith(_DROP_PARAM_PREFIX)
        and k.lower() not in _DROP_PARAM_EXACT
    ]
    return urlunsplit((parts.scheme, parts.netloc, parts.path,
                       urlencode(kept), ""))


def url_hash(url: str) -> str:
    """正規化後的 SHA1，直接對應資料庫的 article.url_hash 欄位。

    媒體常常改標題但網址不變，用網址當唯一鍵，
    後續同一則再進來時會被判定為「已存在」而非新增，符合交接文件的設計。
    """
    return hashlib.sha1(normalize_url(url).encode("utf-8")).hexdigest()


# ------------------------------------------------------------------ 自我測試

if __name__ == "__main__":
    pairs = [
        # 應該併：同一件事的不同寫法
        ("台積電(2330)董事會通過先進封裝擴產案",
         "〈快訊〉台積電董事會通過先進封裝擴產案"),
        ("【MoneyDJ】外資調升台積電目標價至2000元",
         "外資調升台積電目標價至2000元"),
        ("日月光向牧德購買AOI和AVI機器設備　因應產能擴充",
         "日月光投控購買牧德AOI、AVI設備 因應產能擴充"),
        # 不該併：主體像但事情不同
        ("外資調升台積電目標價至2000元",
         "外資調降台積電目標價至1500元"),
        ("台積電法說會釋出資本支出上修訊號",
         "台積電董事長魏哲家出席論壇"),
    ]
    print("=== 標題相似度 ===")
    for a, b in pairs:
        na, nb = normalize_title(a), normalize_title(b)
        print(f"{title_similarity(na, nb):.3f}  {a[:26]} ／ {b[:26]}")
        print(f"        正規化後：{na[:40]}")
        print(f"                  {nb[:40]}")

    print("\n=== 網址正規化 ===")
    urls = [
        "https://www.cna.com.tw/news/afe/202609110187.aspx?utm_source=fb&fbclid=abc",
        "http://cna.com.tw/news/afe/202609110187.aspx/",
        "https://tw.stock.yahoo.com/news/abc-123.html/amp",
        "https://tw.stock.yahoo.com/news/abc-123.html",
    ]
    for u in urls:
        print(f"{url_hash(u)[:10]}  {normalize_url(u)}")
