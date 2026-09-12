"""
mops_fetch.py — 抓取公開資訊觀測站重大訊息，寫入本地資料庫

重要：TWSE / TPEx 的 OpenAPI 提供的是「當下快照」而非歷史資料庫，
      舊公告會滾掉。必須定時輪詢並自行保存，這支程式的存在意義就在這裡。

用法：
    python mops_fetch.py                 # 抓一次
    python mops_fetch.py --stats         # 看資料庫現況

產出：
    news.db   SQLite 資料庫，含 article / event / event_stock 三張表
"""

import argparse
import hashlib
import re
import sqlite3
import ssl
import sys
from datetime import datetime, timezone, timedelta

from netutil import fetch_json

from link import Linker

TPE = timezone(timedelta(hours=8))
DB_PATH = "news.db"

# 同一份資料在兩個交易所有不同路徑，且官方偶爾調整，逐一嘗試
MOPS_ENDPOINTS = [
    ("TWSE", "https://openapi.twse.com.tw/v1/opendata/t187ap04_L"),
    ("TPEx", "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap04_O"),
    ("TPEx", "https://www.tpex.org.tw/openapi/v1/opendata/t187ap04_O"),
]

# ---------------------------------------------------------------- 事件分類

EVENT_RULES = [
    ("revenue", r"營業收入|月營收|自結"),
    ("earnings", r"財務報告|財報|合併財務|每股盈餘"),
    ("conference", r"法人說明會|法說會|業績發表"),
    ("dividend", r"股利|配息|配股|現金增資|減資|買回.*股份"),
    ("ma", r"併購|合併|收購|處分.*股權|取得.*股權|轉投資"),
    ("capacity", r"擴產|擴廠|新建|產能|廠房|(購置|取得|購買).{0,8}(設備|資產|土地|廠房)"),
    ("order", r"接獲.*訂單|簽訂.*合約|策略聯盟|合作意向|得標"),
    ("legal", r"訴訟|裁罰|罰鍰|違反|假扣押|檢調|搜索"),
    # 放最後且收窄：MOPS 幾乎所有公告都以「董事會決議」開頭，
    # 光比對「董事」會把擴產、股利、併購全部誤判成人事案
    ("personnel", r"辭任|解任|補選|改選|新任|委任|(董事長|總經理|財務長|經理人|監察人).{0,6}(異動|變動|異常)"),
]


def classify(subject):
    for etype, pat in EVENT_RULES:
        if re.search(pat, subject):
            return etype
    return "other"


# ---------------------------------------------------------------- 資料庫

SCHEMA = """
CREATE TABLE IF NOT EXISTS article (
    article_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id    TEXT NOT NULL,
    source_tier  INTEGER NOT NULL,
    url_hash     TEXT NOT NULL UNIQUE,
    url          TEXT,
    title        TEXT NOT NULL,
    summary      TEXT,
    published_at TEXT,
    fetched_at   TEXT NOT NULL,
    event_id     INTEGER
);

CREATE TABLE IF NOT EXISTS event (
    event_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    headline         TEXT NOT NULL,
    event_type       TEXT NOT NULL,
    sentiment        INTEGER,
    confidence       REAL,
    is_unverified    INTEGER DEFAULT 0,
    primary_stock_id TEXT,
    first_seen_at    TEXT NOT NULL,
    last_seen_at     TEXT NOT NULL,
    source_count     INTEGER DEFAULT 1,
    best_tier        INTEGER DEFAULT 1,
    status           TEXT DEFAULT 'active'
);

CREATE TABLE IF NOT EXISTS event_stock (
    event_id  INTEGER NOT NULL,
    stock_id  TEXT NOT NULL,
    relevance TEXT NOT NULL,
    PRIMARY KEY (event_id, stock_id)
);

CREATE INDEX IF NOT EXISTS idx_event_time  ON event(first_seen_at);
CREATE INDEX IF NOT EXISTS idx_es_stock    ON event_stock(stock_id);
"""


def open_db(path=DB_PATH):
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    return conn


# ---------------------------------------------------------------- 抓取

def pick(row, *keys):
    for k in keys:
        v = row.get(k)
        if v not in (None, ""):
            return str(v).strip()
    return None


def roc_to_iso(date_str, time_str=None):
    """民國日期轉 ISO。1150908 -> 2026-09-08。西元格式則原樣解析。"""
    if not date_str:
        return None
    d = re.sub(r"\D", "", date_str)
    try:
        if len(d) == 7:
            y, m, dd = int(d[:3]) + 1911, int(d[3:5]), int(d[5:7])
        elif len(d) == 8:
            y, m, dd = int(d[:4]), int(d[4:6]), int(d[6:8])
        else:
            return None
    except ValueError:
        return None

    hh = mm = ss = 0
    if time_str:
        t = re.sub(r"\D", "", time_str).ljust(6, "0")[:6]
        hh, mm, ss = int(t[:2]), int(t[2:4]), int(t[4:6])
    try:
        return datetime(y, m, dd, hh, mm, ss, tzinfo=TPE).astimezone(
            timezone.utc
        ).isoformat()
    except ValueError:
        return None


def fetch_endpoint(url):
    # 櫃買憑證鏈不完整，允許降級重試（見 netutil.py）
    insecure_ok = "tpex.org.tw" in url
    data, _ = fetch_json(url, allow_insecure=insecure_ok)
    return data if isinstance(data, list) else []


def collect():
    rows, seen_urls = [], set()
    for market, url in MOPS_ENDPOINTS:
        if url in seen_urls:
            continue
        seen_urls.add(url)
        try:
            data = fetch_endpoint(url)
        except Exception as e:
            print(f"  [略過] {url}\n         {type(e).__name__}: {e}", file=sys.stderr)
            continue
        if not data:
            continue
        print(f"  {market} {len(data)} 筆  {url}")
        for row in data:
            rows.append((market, row))
    return rows


# ---------------------------------------------------------------- 主流程

def ingest(conn, linker, rows):
    now = datetime.now(timezone.utc).isoformat()
    inserted = skipped = unmatched = 0

    for market, row in rows:
        code = pick(row, "公司代號", "SecuritiesCompanyCode", "Code")
        subject = pick(row, "主旨", "Subject", "主旨 ")
        if not code or not subject:
            continue

        pub = roc_to_iso(
            pick(row, "發言日期", "出表日期", "DateOfSpeech"),
            pick(row, "發言時間", "TimeOfSpeech"),
        )

        raw_key = f"mops|{code}|{pub or ''}|{subject}"
        url_hash = hashlib.sha1(raw_key.encode("utf-8")).hexdigest()

        cur = conn.execute("SELECT 1 FROM article WHERE url_hash = ?", (url_hash,))
        if cur.fetchone():
            skipped += 1
            continue

        # MOPS 公告本身就是一則事件，來源即主角，不必靠標題猜
        hits = linker.link(subject)
        stocks = [(code, "primary")] + [
            (s, r) for s, r in hits if s != code
        ] if code in linker.aliases else hits

        if not stocks:
            unmatched += 1
            continue

        etype = classify(subject)
        cur = conn.execute(
            """INSERT INTO event (headline, event_type, is_unverified,
                                  primary_stock_id, first_seen_at, last_seen_at,
                                  source_count, best_tier)
               VALUES (?,?,?,?,?,?,1,1)""",
            (subject, etype, 0, stocks[0][0], pub or now, pub or now),
        )
        event_id = cur.lastrowid

        conn.executemany(
            "INSERT OR IGNORE INTO event_stock VALUES (?,?,?)",
            [(event_id, sid, rel) for sid, rel in stocks],
        )
        conn.execute(
            """INSERT INTO article (source_id, source_tier, url_hash, url,
                                    title, published_at, fetched_at, event_id)
               VALUES ('mops',1,?,NULL,?,?,?,?)""",
            (url_hash, subject, pub, now, event_id),
        )
        inserted += 1

    conn.commit()
    return inserted, skipped, unmatched


def show_stats(conn):
    q = conn.execute
    print("事件總數：", q("SELECT COUNT(*) FROM event").fetchone()[0])
    print("\n依類型：")
    for t, n in q(
        "SELECT event_type, COUNT(*) FROM event GROUP BY 1 ORDER BY 2 DESC"
    ):
        print(f"  {t:<12} {n}")
    print("\n最近 10 則：")
    for h, s, t in q(
        """SELECT headline, primary_stock_id, event_type
           FROM event ORDER BY first_seen_at DESC LIMIT 10"""
    ):
        print(f"  [{s}] {t:<11} {h[:48]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--aliases", default="aliases.json")
    ap.add_argument("--stats", action="store_true")
    args = ap.parse_args()

    conn = open_db(args.db)

    if args.stats:
        show_stats(conn)
        return

    try:
        linker = Linker(args.aliases)
    except FileNotFoundError:
        print(f"找不到 {args.aliases}，請先執行 build_universe.py", file=sys.stderr)
        sys.exit(1)

    print("抓取重大訊息…")
    rows = collect()
    if not rows:
        print("沒有拿到任何資料，可能是非交易日，或端點路徑需要更新。")
        return

    ins, skip, unm = ingest(conn, linker, rows)
    print(f"\n新增 {ins} 則　已存在 {skip} 則　不在前150名單 {unm} 則")
    print(f"資料庫：{args.db}")


if __name__ == "__main__":
    main()
