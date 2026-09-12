"""
report.py — 從 news.db 產生可讀的每日摘要

輸出 digest.md，GitHub 網頁上會自動渲染成表格，手機也能看。

用法：
    python report.py                # 最近 24 小時
    python report.py --days 3       # 最近 3 天
"""

import argparse
import csv
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone, timedelta

TPE = timezone(timedelta(hours=8))

TYPE_LABEL = {
    "revenue": "營收",
    "earnings": "財報",
    "conference": "法說會",
    "dividend": "股利增減資",
    "ma": "併購轉投資",
    "capacity": "產能擴廠",
    "order": "訂單合約",
    "personnel": "人事",
    "legal": "訴訟裁罰",
    "policy": "產業政策",
    "other": "其他",
}

# 這幾類在版面上優先，其餘按時間排
PRIORITY = ["earnings", "revenue", "conference", "order", "capacity", "ma"]


def load_names(path="universe.csv"):
    names, ranks = {}, {}
    try:
        with open(path, encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                names[row["stock_id"]] = row["name_short"]
                ranks[row["stock_id"]] = int(row["cap_rank"])
    except (FileNotFoundError, KeyError):
        pass
    return names, ranks


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="news.db")
    ap.add_argument("--days", type=int, default=1)
    ap.add_argument("--out", default="digest.md")
    args = ap.parse_args()

    names, ranks = load_names()
    conn = sqlite3.connect(args.db)
    since = (datetime.now(timezone.utc) - timedelta(days=args.days)).isoformat()

    rows = conn.execute(
        """SELECT e.event_id, e.headline, e.event_type, e.primary_stock_id,
                  e.first_seen_at, e.source_count
           FROM event e
           WHERE e.first_seen_at >= ? AND e.status = 'active'
           ORDER BY e.first_seen_at DESC""",
        (since,),
    ).fetchall()

    by_stock = defaultdict(list)
    for r in rows:
        by_stock[r[3]].append(r)

    now_tpe = datetime.now(TPE)
    lines = [
        f"# 台股消息面摘要",
        "",
        f"產生時間：{now_tpe:%Y-%m-%d %H:%M} (台北)　"
        f"區間：最近 {args.days} 天　"
        f"事件 {len(rows)} 則　涉及 {len(by_stock)} 檔",
        "",
    ]

    if not rows:
        lines += ["", "這段期間沒有名單內個股的公告。若今天是週末或國定假日，這是正常的。"]
    else:
        # 個股依市值排名排序，排名不明的放最後
        order = sorted(by_stock, key=lambda s: ranks.get(s, 9999))

        lines += ["## 依個股", ""]
        for sid in order:
            evs = sorted(
                by_stock[sid],
                key=lambda r: (
                    PRIORITY.index(r[2]) if r[2] in PRIORITY else 99,
                    r[4],
                ),
            )
            nm = names.get(sid, "")
            rk = ranks.get(sid)
            head = f"### {nm} {sid}" + (f"　市值第 {rk} 名" if rk else "")
            lines += [head, ""]
            lines += ["| 時間 | 類型 | 內容 |", "|---|---|---|"]
            for _, headline, etype, _, ts, _ in evs:
                try:
                    t = datetime.fromisoformat(ts).astimezone(TPE).strftime("%m/%d %H:%M")
                except ValueError:
                    t = ts[:16]
                safe = headline.replace("|", "｜")
                lines += [f"| {t} | {TYPE_LABEL.get(etype, etype)} | {safe} |"]
            lines += [""]

        counts = defaultdict(int)
        for r in rows:
            counts[r[2]] += 1
        lines += ["## 事件類型分布", "", "| 類型 | 則數 |", "|---|---|"]
        for etype, n in sorted(counts.items(), key=lambda x: -x[1]):
            lines += [f"| {TYPE_LABEL.get(etype, etype)} | {n} |"]
        lines += [""]

    lines += [
        "---",
        "",
        "資料來源：公開資訊觀測站重大訊息（TWSE / TPEx OpenAPI）。",
        "事件分類為規則式自動判定，僅供整理參考，不構成投資建議。",
    ]

    with open(args.out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"{args.out}　事件 {len(rows)} 則　個股 {len(by_stock)} 檔")


if __name__ == "__main__":
    main()
