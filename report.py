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
import json
import os
import re
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

FOCUS_OFFICIAL = 10   # 焦點區給公告的固定名額
FOCUS_MEDIA = 15      # 焦點區給媒體事件的固定名額

# 同一檔在焦點區最多佔幾列。權值股會被寫進幾乎每一則新聞的標題，
# 不設限的話焦點區永遠是台積電（實測 15 列裡它佔 6 列）。
# 超出的不會消失，在個股區塊照樣看得到。
FOCUS_PER_STOCK = 2

# 法定樣板公告。依規定必須揭露，但資訊量趨近於零，
# 第一次跑真實資料時它們佔滿了整個焦點區（子公司資金貸與、背書保證那一類）。
# 不刪除，只把熱度打折讓它們沉下去，個股區塊裡還是看得到。
ROUTINE = re.compile(
    r"資金貸與|背書保證|處理準則第|公告標準|更正本公司|補充.{0,4}公告"
    r"|受邀參加.{0,12}(法人說明會|論壇|說明會)"
)
ROUTINE_PENALTY = 0.25


UNKNOWN_INDUSTRY = "未分類"


def load_industry_map(path="industry_map.json"):
    """代碼轉名稱。universe.csv 的 industry 欄位存的是證交所代碼（如 24），不是文字。"""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f).get("map", {})
    except (FileNotFoundError, json.JSONDecodeError, AttributeError):
        return {}


def load_names(path="universe.csv", imap=None):
    """回傳 (簡稱, 市值排名, 產業名稱)。

    產業別一直都在 universe.csv 裡，只是先前沒拿來用。
    代碼查不到對照時保留原始代碼並標記，日誌會警告，不會靜靜吃掉。
    """
    imap = imap or {}
    names, ranks, inds = {}, {}, {}
    unknown = set()
    try:
        with open(path, encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                sid = row["stock_id"]
                names[sid] = row["name_short"]
                ranks[sid] = int(row["cap_rank"])
                code = (row.get("industry") or "").strip()
                if not code:
                    inds[sid] = UNKNOWN_INDUSTRY
                elif code in imap:
                    inds[sid] = imap[code]
                else:
                    inds[sid] = f"產業別 {code}"
                    unknown.add(code)
    except (FileNotFoundError, KeyError):
        pass
    return names, ranks, inds, unknown


# 來源短名。版面上「中央社 產經證券」太長，取空白前的第一段就夠識別。
SOURCE_SHORT_OVERRIDE = {"mops": "公告"}
SOURCE_NAME_FALLBACK = {"mops": "公開資訊觀測站重大訊息"}


def load_source_names(path="rss_sources.json"):
    names = dict(SOURCE_NAME_FALLBACK)
    try:
        with open(path, encoding="utf-8") as f:
            for src in json.load(f).get("sources", []):
                names[src["id"]] = src.get("name", src["id"])
    except (FileNotFoundError, json.JSONDecodeError, KeyError, TypeError):
        pass
    return names


def short_source(source_id, names):
    if source_id in SOURCE_SHORT_OVERRIDE:
        return SOURCE_SHORT_OVERRIDE[source_id]
    full = names.get(source_id, source_id)
    return full.split()[0] if full.split() else full


def load_event_sources(conn, event_ids, names):
    """每則事件的代表連結與實際來源媒體。

    來源依 tier 排序，所以第一個就是最權威的那家。
    一件事被多家報導時，版面上顯示「鉅亨網 等 3 家」比單純顯示數字有用得多，
    因為「誰在報」跟「幾家在報」是兩種不同的訊號。

    MOPS 公告沒有穩定的對外連結，url 會是空的，這種情況就不放連結。
    """
    if not event_ids:
        return {}
    out = {}
    qmarks = ",".join("?" * len(event_ids))
    rows = conn.execute(
        f"""SELECT event_id, url, source_id
            FROM article
            WHERE event_id IN ({qmarks})
            ORDER BY source_tier ASC, published_at ASC""",
        list(event_ids),
    )
    for ev, url, src in rows:
        d = out.setdefault(ev, {"url": None, "ids": []})
        if url and not d["url"]:
            d["url"] = url
        if src not in d["ids"]:
            d["ids"].append(src)

    for ev, d in out.items():
        shorts = [short_source(i, names) for i in d["ids"]]
        # 同一家的不同分類 feed（Yahoo 有三個）算同一家，不要重複顯示
        uniq = list(dict.fromkeys(shorts))
        d["outlets"] = uniq
        d["label"] = uniq[0] if len(uniq) == 1 else f"{uniq[0]} 等 {len(uniq)} 家"
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
    """表格欄位淨化。

    MOPS 的主旨裡帶換行字元（公司自己在申報系統裡斷的行），
    直接塞進 Markdown 表格會把那一列撐破，所以所有空白壓成單一空格。
    """
    return re.sub(r"\s+", " ", (s or "").replace("|", "｜")).strip()


def headline_cell(headline, link_info, unverified):
    """標題欄。未證實的消息要一眼看得出來，不能跟公告混在一起。"""
    text = esc(headline)
    if link_info and link_info.get("url"):
        text = f"[{text}]({link_info['url']})"
    if unverified:
        text = "傳聞　" + text
    return text


def write_web(path, rows, links, names, ranks, inds, health, days):
    """輸出 docs/data.json，給 GitHub Pages 上的靜態頁面讀。

    刻意跟 digest.md 分開產生，也刻意跟 index.html 分開：
    介面改版不必重跑管線，資料更新也不會動到介面。
    只輸出標題、時間、來源、連結，跟資料庫裡保存的範圍一致。
    """
    now = datetime.now(timezone.utc)
    events = []
    for ev, headline, etype, sid, ts, cnt, tier, unv in rows:
        link = links.get(ev) or {}
        events.append({
            "id": ev,
            "headline": esc(headline),
            "type": etype,
            "type_label": TYPE_LABEL.get(etype, etype),
            "stock_id": sid,
            "stock_name": names.get(sid, ""),
            "cap_rank": ranks.get(sid),
            "industry": inds.get(sid, UNKNOWN_INDUSTRY),
            "tier": tier,
            "tier_label": TIER_LABEL.get(tier, str(tier)),
            "sources": cnt,
            "unverified": bool(unv),
            "time": ts,
            "heat": round(heat(cnt, tier, ts, now), 4),
            "url": link.get("url"),
            "source": link.get("label", ""),
            "outlets": link.get("outlets", []),
        })

    feeds = []
    for src, last_ok, status, fails, items in health:
        hours = None
        if last_ok:
            try:
                hours = round(
                    (now - datetime.fromisoformat(last_ok)).total_seconds() / 3600, 1)
            except ValueError:
                pass
        feeds.append({"name": src, "last_success": last_ok,
                      "hours_since": hours, "failures": fails or 0})

    n_official = sum(1 for r in rows if r[6] == 1)
    payload = {
        "generated_at": now.isoformat(),
        "days": days,
        "counts": {
            "events": len(rows),
            "stocks": len({r[3] for r in rows}),
            "official": n_official,
            "media": len(rows) - n_official,
            "unverified": sum(1 for r in rows if r[7]),
        },
        "sources": feeds,
        "events": events,
    }

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    return len(events)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="news.db")
    ap.add_argument("--days", type=int, default=1)
    ap.add_argument("--out", default="digest.md")
    ap.add_argument("--web", default="docs/data.json",
                    help="網站資料檔的輸出位置，設成空字串可停用")
    args = ap.parse_args()

    imap = load_industry_map()
    names, ranks, inds, unknown_codes = load_names(imap=imap)
    if unknown_codes:
        print("未知產業代碼：" + "、".join(sorted(unknown_codes))
              + "　請加進 industry_map.json 的 map 區塊")
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

    src_names = load_source_names()
    links = load_event_sources(conn, [r[0] for r in rows], src_names)

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
        def score(r):
            h = heat(r[5], r[6], r[4], now_utc)
            return h * ROUTINE_PENALTY if ROUTINE.search(r[1] or "") else h

        scored = sorted(((score(r), r) for r in rows), key=lambda x: -x[0])

        def pick(items, limit):
            used, out = defaultdict(int), []
            for h, r in items:
                if used[r[3]] >= FOCUS_PER_STOCK:
                    continue
                used[r[3]] += 1
                out.append((h, r))
                if len(out) >= limit:
                    break
            return out

        official = pick([(h, r) for h, r in scored if r[6] == 1], FOCUS_OFFICIAL)
        media = pick([(h, r) for h, r in scored if r[6] != 1], FOCUS_MEDIA)

        # 分成兩張表而不是一張。
        # 原本的寫法是「公告優先、剩下的名額給媒體」，結果 42 則公告把 20 個
        # 名額全部佔滿，媒體事件一則都排不進來，接媒體的意義完全看不到。
        # 公告與媒體本來就是不同性質的東西，熱度分數也不可比
        # （公告永遠只有一個來源），硬排在同一張表本身就是錯的。
        def focus_table(title, items, note):
            out = [f"## {title}", "", note, "",
                   "| 熱度 | 個股 | 類型 | 來源 | 時間 | 內容 |",
                   "|---|---|---|---|---|---|"]
            if not items:
                return [f"## {title}", "", note, "", "（這段期間沒有）", ""]
            for h, (ev, headline, etype, sid, ts, cnt, tier, unv) in items:
                info = links.get(ev) or {}
                out.append(
                    f"| {h:.2f} | {names.get(sid, '')} {sid} "
                    f"| {TYPE_LABEL.get(etype, etype)} | {esc(info.get('label', '—'))} "
                    f"| {fmt_time(ts)} "
                    f"| {headline_cell(headline, links.get(ev), unv)} |")
            return out + [""]

        lines += focus_table(
            "今日焦點　官方公告", official,
            "依規定必須揭露的樣板公告（子公司資金貸與、背書保證等）"
            "熱度已打折，會沉到後面，在個股區塊仍看得到。")
        lines += focus_table(
            "今日焦點　媒體消息", media,
            "熱度 = log(1+來源家數) × 來源權重 × 時間衰減。"
            "家數大於 1 代表多家媒體同時在報，是熱度的主要訊號。")

        # ---------------------------------------------------------- 依產業
        # 「整個族群在動」跟「單一公司出事」意義差很多，
        # 光看依個股的清單分不出來，所以另外聚一層。
        by_ind = defaultdict(list)
        for r in rows:
            by_ind[inds.get(r[3], UNKNOWN_INDUSTRY)].append(r)

        lines += ["## 依產業", "",
                  "消息數多不代表重要，但同一個產業同時冒出好幾檔，"
                  "通常值得回頭看是不是整個族群的事。",
                  "",
                  "| 產業 | 消息 | 個股 | 最受關注的一則 |", "|---|---|---|---|"]
        for ind, evs in sorted(by_ind.items(),
                               key=lambda kv: (-len(kv[1]), kv[0])):
            top = max(evs, key=lambda r: score(r))
            stocks = sorted({r[3] for r in evs},
                            key=lambda s: ranks.get(s, 9999))
            shown = "、".join(names.get(s, s) for s in stocks[:5])
            if len(stocks) > 5:
                shown += f" 等 {len(stocks)} 檔"
            lines += [f"| {ind} | {len(evs)} | {shown} | "
                      f"{headline_cell(top[1], links.get(top[0]), top[7])} |"]
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
            bits = []
            if rk:
                bits.append(f"市值第 {rk} 名")
            ind = inds.get(sid)
            if ind and ind != UNKNOWN_INDUSTRY:
                bits.append(ind)
            lines += [f"### {names.get(sid, '')} {sid}"
                      + ("　" + "　".join(bits) if bits else ""), ""]
            lines += ["| 時間 | 類型 | 來源 | 內容 |", "|---|---|---|---|"]
            for ev, headline, etype, _s, ts, cnt, tier, unv in evs:
                # 來源欄顯示實際媒體而非層級。層級已經由「公告/傳聞」的標記
                # 與排序表達過了，這裡重複一次沒有新資訊；
                # 「是鉅亨還是中央社在報」才是看的時候真正想知道的事。
                info = links.get(ev) or {}
                lines.append(
                    f"| {fmt_time(ts)} | {TYPE_LABEL.get(etype, etype)} "
                    f"| {esc(info.get('label', '—'))} "
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

    if args.web:
        n = write_web(args.web, rows, links, names, ranks, inds,
                      health, args.days)
        print(f"{args.web}　{n} 則")


if __name__ == "__main__":
    main()
