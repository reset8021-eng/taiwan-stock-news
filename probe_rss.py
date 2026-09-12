"""
probe_rss.py — RSS 來源診斷工具

為什麼需要這支：媒體的 RSS 網址沒有標準、會改、會無預警下架，而且很多站
根本沒有公開說明頁。與其由我憑印象給你網址（很可能是錯的），不如讓程式
自己去確認。

用法（在 GitHub Actions 上按 Run workflow 執行 probe-rss.yml）：
    python probe_rss.py              # 測 rss_sources.json 裡所有來源，含未啟用的
    python probe_rss.py --discover   # 另外對 discover_sites 做 RSS 自動探索

自動探索的原理：網頁若有 RSS，慣例會在 <head> 放
    <link rel="alternate" type="application/rss+xml" href="...">
這是 RSS 探索的標準做法，抓首頁把這些 href 撈出來就好，不必猜路徑。

輸出 rss_probe.md，看完把能通的來源在 rss_sources.json 裡把 enabled 改成 true。
這支是診斷工具，不寫入 news.db，可以隨時跑。
"""

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin

import feedparser

from netutil import fetch_bytes

TPE = timezone(timedelta(hours=8))

# <link rel="alternate" type="application/rss+xml" ...> 的寬鬆比對。
# 屬性順序不固定，所以分兩段抓：先找 link 標籤，再看裡面有沒有 rss/atom 型別。
_LINK_TAG = re.compile(r"<link\b[^>]*>", re.IGNORECASE)
_HREF = re.compile(r"""href\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
_TITLE_ATTR = re.compile(r"""title\s*=\s*["']([^"']*)["']""", re.IGNORECASE)
_FEED_TYPE = re.compile(r"application/(rss|atom)\+xml", re.IGNORECASE)


def probe_one(url, timeout=25):
    """回傳一個結果 dict，不丟例外，讓單一來源掛掉不影響整份報告。"""
    out = {"url": url, "ok": False, "detail": "", "items": 0,
           "title": "", "newest": "", "etag": False}
    try:
        status, content, headers = fetch_bytes(
            url, extra_headers={"Accept": "application/rss+xml, */*",
             "User-Agent": ("Mozilla/5.0 (compatible; tw-stock-news/0.3; "
                            "+https://github.com/reset8021-eng/taiwan-stock-news)")},
            timeout=timeout)
    except Exception as e:
        out["detail"] = f"{type(e).__name__}: {str(e)[:110]}"
        return out

    out["etag"] = bool(headers.get("ETag") or headers.get("Last-Modified"))
    ctype = headers.get("Content-Type", "")

    feed = feedparser.parse(content)
    entries = feed.entries or []
    out["items"] = len(entries)
    out["title"] = (getattr(feed.feed, "title", "") or "")[:40]

    if not entries:
        bad = str(getattr(feed, "bozo_exception", ""))[:80]
        out["detail"] = f"HTTP {status}，Content-Type {ctype[:30]}，剖析出 0 筆"
        if bad:
            out["detail"] += f"，XML 異常：{bad}"
        return out

    # 有沒有可用的時間戳，直接決定熱度公式能不能算
    newest = None
    for e in entries:
        st = e.get("published_parsed") or e.get("updated_parsed")
        if st:
            try:
                dt = datetime(*st[:6], tzinfo=timezone.utc)
            except (TypeError, ValueError):
                continue
            newest = dt if newest is None or dt > newest else newest
    if newest:
        out["newest"] = newest.astimezone(TPE).strftime("%Y-%m-%d %H:%M")
        age_h = (datetime.now(timezone.utc) - newest).total_seconds() / 3600
        out["detail"] = f"最新一則距今 {age_h:.1f} 小時"
    else:
        out["detail"] = "項目沒有可用的發布時間，熱度衰減會退回抓取時間"

    out["ok"] = True
    return out


def discover(site, timeout=25):
    """從網頁 <head> 的 link 標籤找出 RSS 網址。回傳 [(標題, 絕對網址)]。"""
    try:
        _, content, _ = fetch_bytes(site, timeout=timeout)
    except Exception as e:
        return [], f"{type(e).__name__}: {str(e)[:100]}"

    html = content.decode("utf-8", errors="ignore")
    found = []
    for tag in _LINK_TAG.findall(html)[:400]:
        if not _FEED_TYPE.search(tag):
            continue
        m = _HREF.search(tag)
        if not m:
            continue
        t = _TITLE_ATTR.search(tag)
        found.append(((t.group(1) if t else "")[:30], urljoin(site, m.group(1))))

    # 有些站把 RSS 放在一般 <a> 連結而不是 <head>，補撈明顯是 feed 的連結
    for m in re.finditer(r"""href\s*=\s*["']([^"']*(?:rss|feed|atom)[^"']*)["']""",
                         html, re.IGNORECASE):
        u = urljoin(site, m.group(1))
        if u.lower().endswith((".css", ".js", ".png", ".jpg", ".svg")):
            continue
        if all(u != f[1] for f in found):
            found.append(("（頁面連結）", u))

    seen, uniq = set(), []
    for t, u in found:
        if u not in seen:
            seen.add(u)
            uniq.append((t, u))
    return uniq[:25], ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sources", default="rss_sources.json")
    ap.add_argument("--out", default="rss_probe.md")
    ap.add_argument("--discover", action="store_true",
                    help="另外對 discover_sites 做 RSS 自動探索")
    args = ap.parse_args()

    with open(args.sources, encoding="utf-8") as f:
        cfg = json.load(f)

    now = datetime.now(TPE)
    lines = [
        "# RSS 來源探測結果",
        "",
        f"執行時間：{now:%Y-%m-%d %H:%M}（台北）",
        "",
        "「可用」代表網址通、回傳的內容能剖析出項目。",
        "要啟用某個來源，把 rss_sources.json 裡它的 enabled 改成 true。",
        "",
        "## 設定檔內的來源",
        "",
        "| 狀態 | id | 目前啟用 | 項目數 | 條件式請求 | 說明 |",
        "|---|---|---|---|---|---|",
    ]

    for src in cfg.get("sources", []):
        r = probe_one(src["url"])
        mark = "可用" if r["ok"] else "不可用"
        cond = "支援" if r["etag"] else "不支援"
        lines.append(
            f"| {mark} | {src['id']} | {'是' if src.get('enabled') else '否'} "
            f"| {r['items']} | {cond} | {r['detail'].replace('|', '｜')} |")
        print(f"{mark:<4} {src['id']:<18} {r['items']:>3} 筆  {r['detail'][:70]}",
              file=sys.stderr)

    lines += [
        "",
        "「條件式請求」欄位是指來源有沒有回 ETag 或 Last-Modified。",
        "不支援的來源每次都得整份下載，往後擴充來源數時要優先控制它的抓取頻率。",
        "",
    ]

    if args.discover:
        lines += ["## 自動探索", "",
                  "以下是從各站首頁的 link 標籤撈出來的 feed 網址，未經內容驗證。",
                  "挑看起來對的貼回 rss_sources.json，再跑一次這支程式確認。", ""]
        for site in cfg.get("discover_sites", []):
            found, err = discover(site)
            lines += [f"### {site}", ""]
            if err:
                lines += [f"抓取失敗：{err}", ""]
                continue
            if not found:
                lines += ["沒有找到任何 feed 連結。", ""]
                continue
            lines += ["| 標題 | 網址 |", "|---|---|"]
            lines += [f"| {t or '（無）'} | {u} |" for t, u in found]
            lines += [""]

    with open(args.out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"\n已寫出 {args.out}")


if __name__ == "__main__":
    main()
