"""
report.py — 從 news.db 產生可讀的每日摘要

輸出 digest.md，GitHub 網頁上會自動渲染成表格，手機也能看。

用法：
    python report.py                # 最近 24 小時
    python report.py --days 3       # 最近 3 天

接上媒體 RSS 之後這份報表多了三件事：

1. 「今日焦點」用熱度排序。事件量從一天二三十則變成上百則之後，
   照個股排會看不完，需要一個「先看什麼」的入口。
2. 每則事件顯示來源家數。十家媒體都報的事，跟只有一家提到的事，
   在版面上必須看得出差別，這是接媒體最主要的目的。
3. 官方公告（tier 1）在焦點區強制保留，不被媒體熱度擠掉。
   事實以公告為準，媒體只是血肉。
"""

import argparse
import csv
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from aggregate import heat

TPE = timezone(timedelta(hours=8))

TYPE_LABEL = {
    "revenue": "營收",
    "earnings": "財報",
    "conference": "法說會",
    "dividend": "股利增減資",
    "ma": "併購轉投資",
    "capacity": "產能擴廠",
    "order": "訂單合約",
    "rating": "評等目標價",
    "personnel": "人事",
    "legal": "訴訟裁罰",
    "policy": "產業政策",
    "other": "其他",
}

TIER_LABEL = {1: "公告", 2: "財經媒體", 3: "一般新聞", 4: "其他"}

# 這幾類在個股區塊內優先，其餘按時間排
PRIORITY = ["earnings", "revenue", "conference", "rating", "order", "capacity", "ma"]

FOCUS_LIMIT = 20


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


def load_best_links(conn, event_ids):
    """每則事件挑一個代表連結：來源層級最權威、其次最早發布的那一篇。

    MOPS 的 article 沒有 url（公告本身沒有穩定的對外連結），
    這種情況會退回找次一級來源的連結，都沒有就不放連結。
    """
    if not event_ids:
        return {}
    out = {}
    qmarks = ",".join("?" * len(event_ids))
    rows = conn.execute(
        f"""SELECT event_id, url, source_id
            FROM article
            WHERE event_id IN ({qmarks}) AND url IS NOT NULL AND url != ''
            ORDER BY source_tier ASC, published_at ASC""",
        list(event_ids),
    )
    for ev, url, src in rows:
        out.setdefault(ev, (url, src))
    return out


def load_feed_health(conn):
    try:
        return conn.execute(
            """SELECT source_id, last_success_at, last_status,
                      consecutive_failures, last_item_count
               FROM feed_state ORDER BY source_id"""
        ).fetchall()
    except sqlite3.OperationalError:
        # rss_fetch.py 還沒跑過，沒有這張表，這不是錯誤
        return []


def fmt_time(ts):
    try:
        return datetime.fromisoformat(ts).astimezone(TPE).strftime("%m/%d %H:%M")
    except (ValueError, TypeError):
        return (ts or "")[:16]


def esc(s):
    return (s or "").replace("|", "｜")


def headline_cell(headline, link_info, unverified):
    """標題欄。未證實的消息要一眼看得出來，不能跟公告混在一起。"""
    text = esc(headline)
    if link_info:
        url, _src = link_info
        text = f"[{text}]({url})"
    if unverified:
        text = "傳聞　" + text
    return text


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="news.db")
    ap.add_argument("--days", type=int, default=1)
    ap.add_argument("--out", default="digest.md")
    args = ap.parse_args()

    names, ranks = load_names()
    conn = sqlite3.connect(args.db)
    now_utc = datetime.now(timezone.utc)
    since = (now_utc - timedelta(days=args.days)).isoformat()

    rows = conn.execute(
        """SELECT e.event_id, e.headline, e.event_type, e.primary_stock_id,
                  e.first_seen_at, e.source_count, e.best_tier, e.is_unverified
           FROM event e
           WHERE e.first_seen_at >= ? AND e.status = 'active'
           ORDER BY e.first_seen_at DESC""",
        (since,),
    ).fetchall()

    links = load_best_links(conn, [r[0] for r in rows])

    by_stock = defaultdict(list)
    for r in rows:
        by_stock[r[3]].append(r)

    now_tpe = datetime.now(TPE)
    n_official = sum(1 for r in rows if r[6] == 1)
    n_unverified = sum(1 for r in rows if r[7])

    lines = [
        "# 台股消息面摘要",
        "",
        f"產生時間：{now_tpe:%Y-%m-%d %H:%M}（台北）　區間：最近 {args.days} 天",
        "",
        f"事件 {len(rows)} 則　涉及 {len(by_stock)} 檔　"
        f"官方公告 {n_official} 則　媒體事件 {len(rows) - n_official} 則　"
        f"未證實 {n_unverified} 則",
        "",
    ]

    if not rows:
        lines += ["", "這段期間沒有名單內個股的消息。"
                  "若今天是週末或國定假日，這是正常的。"]
    else:
        # ---------------------------------------------------------- 今日焦點
        scored = [(heat(r[5], r[6], r[4], now_utc), r) for r in rows]
        scored.sort(key=lambda x: -x[0])

        # tier 1 強制保留：先把公告挑出來，再用熱度補滿剩下的名額
        official = [(h, r) for h, r in scored if r[6] == 1]
        others = [(h, r) for h, r in scored if r[6] != 1]
        focus = official[:FOCUS_LIMIT] + others[:max(0, FOCUS_LIMIT - len(official))]
        focus.sort(key=lambda x: -x[0])

        lines += [
            "## 今日焦點",
            "",
            "熱度 = log(1+來源家數) × 來源權重 × 時間衰減。"
            "官方公告一律保留，不受熱度排擠。",
            "",
            "| 熱度 | 個股 | 類型 | 來源 | 家數 | 時間 | 內容 |",
            "|---|---|---|---|---|---|---|",
        ]
        for h, (ev, headline, etype, sid, ts, cnt, tier, unv) in focus:
            lines.append(
                f"| {h:.2f} | {names.get(sid, '')} {sid} "
                f"| {TYPE_LABEL.get(etype, etype)} | {TIER_LABEL.get(tier, tier)} "
                f"| {cnt} | {fmt_time(ts)} "
                f"| {headline_cell(headline, links.get(ev), unv)} |"
            )
        lines += [""]

        # ---------------------------------------------------------- 依個股
        order = sorted(by_stock, key=lambda s: ranks.get(s, 9999))

        lines += ["## 依個股", ""]
        for sid in order:
            evs = sorted(
                by_stock[sid],
                key=lambda r: (
                    PRIORITY.index(r[2]) if r[2] in PRIORITY else 99,
                    -r[5],
                    r[4],
                ),
            )
            rk = ranks.get(sid)
            lines += [f"### {names.get(sid, '')} {sid}"
                      + (f"　市值第 {rk} 名" if rk else ""), ""]
            lines += ["| 時間 | 類型 | 來源 | 家數 | 內容 |", "|---|---|---|---|---|"]
            for ev, headline, etype, _s, ts, cnt, tier, unv in evs:
                lines.append(
                    f"| {fmt_time(ts)} | {TYPE_LABEL.get(etype, etype)} "
                    f"| {TIER_LABEL.get(tier, tier)} | {cnt} "
                    f"| {headline_cell(headline, links.get(ev), unv)} |"
                )
            lines += [""]

        # ---------------------------------------------------------- 分布
        counts = defaultdict(int)
        for r in rows:
            counts[r[2]] += 1
        lines += ["## 事件類型分布", "", "| 類型 | 則數 |", "|---|---|"]
        for etype, n in sorted(counts.items(), key=lambda x: -x[1]):
            lines += [f"| {TYPE_LABEL.get(etype, etype)} | {n} |"]
        lines += [""]

    # -------------------------------------------------------------- 來源健康
    health = load_feed_health(conn)
    if health:
        lines += ["## 來源狀態", "",
                  "超過 24 小時沒有成功抓取的來源會標示出來。"
                  "台灣媒體的 RSS 會無預警下架，這一欄要定期看。",
                  "", "| 來源 | 最後成功 | 連續失敗 | 上次筆數 | 狀態 |",
                  "|---|---|---|---|---|"]
        for src, last_ok, status, fails, items in health:
            flag = ""
            if last_ok:
                try:
                    hrs = (now_utc - datetime.fromisoformat(last_ok)).total_seconds() / 3600
                    if hrs > 24:
                        flag = f"　超過 {hrs:.0f} 小時未更新"
                except ValueError:
                    pass
            else:
                flag = "　從未成功"
            lines.append(
                f"| {src} | {fmt_time(last_ok) if last_ok else '—'}{flag} "
                f"| {fails or 0} | {items or 0} | {esc(status)} |")
        lines += [""]

    lines += [
        "---",
        "",
        "資料來源：公開資訊觀測站重大訊息（TWSE / TPEx OpenAPI）與各媒體 RSS。",
        "本頁僅保存標題、時間、來源與連結，不重製新聞內文，內容請點連結至原站閱讀。",
        "事件分類與聚合為規則式自動判定，標示「傳聞」者為未經證實的消息。",
        "整理參考用，不構成投資建議。",
    ]

    with open(args.out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"{args.out}　事件 {len(rows)} 則　個股 {len(by_stock)} 檔")


if __name__ == "__main__":
    main()
