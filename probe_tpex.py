"""
probe_tpex.py — 探測櫃買中心 OpenAPI 有哪些可用端點

build_universe.py 裡的櫃買網址是推測來的，從未驗證。
這支程式去讀櫃買的 OpenAPI 規格檔，列出真實存在的端點，
並實際打幾個看起來像「公司基本資料」和「每日收盤」的，
印出前兩筆與欄位名，讓我們知道該怎麼對應欄位。

用法：python probe_tpex.py
"""

import json
import sys

from netutil import fetch_json

SPEC_URLS = [
    "https://www.tpex.org.tw/openapi/swagger.json",
    "https://www.tpex.org.tw/openapi/v1/swagger.json",
    "https://www.tpex.org.tw/openapi/openapi.json",
    "https://www.tpex.org.tw/openapi/v3/api-docs",
]

KEYWORDS = ["t187ap03", "t187ap04", "daily_close", "mainboard", "basic", "quote"]


def main():
    spec = None
    for url in SPEC_URLS:
        try:
            data, level = fetch_json(url, allow_insecure=True)
            print(f"✓ 取得規格檔：{url}（驗證層級 {level}）\n")
            spec = data
            break
        except Exception as e:
            print(f"✗ {url}\n  {type(e).__name__}: {str(e)[:90]}", file=sys.stderr)

    if not isinstance(spec, dict) or "paths" not in spec:
        print("\n找不到規格檔，改為直接試打候選端點。\n")
        probe_candidates()
        return

    paths = sorted(spec["paths"].keys())
    print(f"櫃買 OpenAPI 共 {len(paths)} 個端點\n")

    hits = [p for p in paths if any(k in p.lower() for k in KEYWORDS)]
    print("=== 可能相關的端點 ===")
    for p in hits:
        print(" ", p)

    print("\n=== 全部端點 ===")
    for p in paths:
        print(" ", p)

    print("\n=== 實際試打相關端點 ===")
    for p in hits[:8]:
        probe("https://www.tpex.org.tw/openapi" + p)


def probe_candidates():
    for url in [
        "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O",
        "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes",
        "https://www.tpex.org.tw/openapi/v1/opendata/t187ap03_O",
    ]:
        probe(url)


def probe(url):
    print(f"\n--- {url}")
    try:
        data, level = fetch_json(url, allow_insecure=True)
    except Exception as e:
        print(f"    失敗 {type(e).__name__}: {str(e)[:90]}")
        return

    print(f"    層級 {level}　型別 {type(data).__name__}", end="")
    if isinstance(data, list):
        print(f"　筆數 {len(data)}")
        if data:
            print(f"    欄位：{list(data[0].keys())}")
            print(f"    首筆：{json.dumps(data[0], ensure_ascii=False)[:200]}")
    else:
        print()
        print(f"    內容：{json.dumps(data, ensure_ascii=False)[:200]}")


if __name__ == "__main__":
    main()
