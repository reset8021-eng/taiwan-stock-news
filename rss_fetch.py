"""
rss_fetch.py — 抓取媒體 RSS，對股、分類、聚合成事件，寫入 news.db

用法：
    python rss_fetch.py                      # 照設定檔抓所有 enabled 的來源
    python rss_fetch.py --only cna_finance   # 只抓一個來源，調參數時用
    python rss_fetch.py --force              # 忽略最小間隔限制
    python rss_fetch.py --sim-merge 0.5      # 臨時改聚合門檻，不用改程式

產出：
    news.db              新增 article / event / event_stock / feed_state 資料
    cluster_audit.md     聚合判斷的稽核檔，用來調門檻，會自動提交

版權紅線（交接文件第五節第 3 點）：
    這支程式只寫入標題、時間、來源、連結。
    RSS 的 description 欄位只在記憶體中用來輔助對股，不寫進資料庫、不輸出。
    article.summary 一律留空，等之後接 LLM 產生改寫版本再填。
"""

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone

import feedparser

import aggregate
from link import Linker
from mops_fetch import open_db
from netutil import fetch_bytes
from textnorm import clean_url, url_hash

TPE = timezone(timedelta(hours=8))

# 若某個來源仍然回 403，在 rss_sources.json 該來源加上 "user_agent": "..." 覆寫。
DEFAULT_UA = (
    "Mozilla/5.0 (compatible; tw-stock-news/0.3; "
    "+https://github.com/reset8021-eng/taiwan-stock-news)"
)

# ---------------------------------------------------------------- 事件分類

# mops_fetch.py 的規則是照公告用語寫的（「董事會決議通過」那種），
# 媒體標題的講法完全不同，需要另一套。順序有意義，先命中先算。
MEDIA_RULES = [
    # rating 是這次接媒體最主要的收穫：MOPS 一則都沒有，但常常最早動到股價
    ("rating", r"目標價|評等|調升|調降|上修|下修|首評|喊上|喊到|看上|投顧|分析師|外資看|大摩|小摩|高盛|花旗|美銀|野村|里昂|郭明錤"),
    ("conference", r"法說|法人說明|股東會|業績發表|業績說明"),
    ("revenue", r"營收|自結|出貨|拉貨|接單|稼動率"),
    ("earnings", r"財報|每股盈餘|EPS|毛利率|營益率|獲利|虧損|轉盈|轉虧"),
    ("order", r"訂單|大單|得標|標案|簽約|合約|策略聯盟|合作|供應鏈|打入|切入|認證"),
    ("capacity", r"擴產|擴廠|新廠|產能|投產|量產|設備|資本支出|建廠"),
    ("dividend", r"股利|配息|除息|除權|現金增資|減資|庫藏股|買回"),
    ("ma", r"併購|收購|合併|入股|參股|轉投資|分拆|私有化"),
    ("legal", r"訴訟|求償|專利戰|裁罰|罰鍰|檢調|搜索|判決|假扣押"),
    ("personnel", r"接班|請辭|辭任|請辭獲准|新任|升任|人事|董事長.{0,4}異動|總經理.{0,4}異動"),
    ("policy", r"關稅|出口管制|禁令|補助|政策|法規|經濟部|金管會|央行|行政院|立法院|國安|匯率"),
]
MEDIA_RULES = [(t, re.compile(p)) for t, p in MEDIA_RULES]


def classify_media(title: str) -> str:
    for etype, pat in MEDIA_RULES:
        if pat.search(title):
            return etype
    return "other"


# ---------------------------------------------------------------- 大盤噪音

# 2026-09-12 加入。第一次跑真實資料時，台積電底下出現七八則這種東西：
#   「國際油價回落『美股終結連4黑』 台積電ADR漲1.22%」
#   「升息預期已提前反映！美股道瓊終止連4跌強彈509點、台積電ADR漲1.22%」
#   「台積電ADR11日上漲5.21美元漲幅1.22%折台股2741.37元」
# 這些是大盤與美股行情播報，台積電只是被拿來當溫度計，不是它的消息。
# 同一個交易時段會被十幾家媒體各寫一則，不擋掉會把權值股的版面整個淹掉，
# 而且會讓聚合門檻的校準失真（拿噪音在調參數）。

# 純報價機器人：「台積電ADR11日上漲5.21美元漲幅1.22%折台股2741.37元」
_ADR_QUOTE = re.compile(r"ADR\s*\d{0,2}\s*日?\s*(上漲|下跌|持平|漲|跌)")

# 公司名後面緊接這些字，代表提到的是衍生商品或海外掛牌，不是公司本身
_QUOTE_TAIL = re.compile(r"^\s*(ADR|期貨|盤後|夜盤)", re.IGNORECASE)

# 大盤字眼。單獨出現不足以判定，要跟「公司只以報價形式出現」一起用
_MARKET_WORDS = re.compile(
    r"美股|道瓊|那斯達克|費城半導體|費半|標普|S&P|加權指數|大盤|台指期"
    r"|夜盤|盤前|盤後|開盤|收盤|四大指數"
)


# 公司名出現在社會新聞或花絮裡。2026-09-12 實測撿到
# 「台積電工程師『輪夜班難顧孩』？離婚爭2幼子親權竟輸了」這種。
# 條件寫窄：必須是明確的私人生活字眼，不碰公司本身的訴訟與人事。
_OFFTOPIC = re.compile(
    r"離婚|親權|扶養費|外遇|告別式|喪禮|婚禮|小三|命理|星座|樂透|中獎"
)


def is_offtopic(title: str) -> bool:
    return bool(_OFFTOPIC.search(title))


def is_market_noise(title: str, primary_id: str, linker: Linker) -> bool:
    """判斷這則是不是「大盤行情播報」而非個股消息。

    判定條件刻意設得窄，寧可漏擋也不要誤擋個股消息：
      A. 標題符合 ADR 報價機器人的格式，直接擋。
      B. 標題有大盤字眼，而且主角公司在標題裡「每一次」出現都緊接著
         ADR 或期貨這類字，代表它只是被當成行情標的提及。

    反例（必須放行）：
      「台積電8月營收又破紀錄」          公司名後面不是 ADR
      「台積電尾盤爆殺單！跌40元力守月線」 有「尾盤」但公司是主角
    """
    if _ADR_QUOTE.search(title):
        return True
    if not _MARKET_WORDS.search(title):
        return False

    aliases = linker.aliases.get(primary_id, [])
    if not aliases:
        return False

    # 長別名優先。別名表裡「台積」是「台積電」的子字串，若讓短的先命中，
    # 「台積電ADR」會被切成「台積」+「電ADR」，後綴檢查就永遠失敗。
    # 正則的交替是最左優先，把長的排前面即可取得最長匹配。
    pat = re.compile("|".join(re.escape(a) for a in
                              sorted(aliases, key=len, reverse=True)))
    seen = False
    for m in pat.finditer(title):
        seen = True
        if not _QUOTE_TAIL.match(title[m.end():m.end() + 4]):
            return False  # 有一次是正常提及，就不算純報價
    return seen


# ---------------------------------------------------------------- 誤配確認

# 財經語境字眼。出現任一個，才承認地雷別名的配對。
# 這串刻意涵蓋得寬，因為它的工作是「排除完全無關的新聞」，
# 不是「挑出重要新聞」；抓太緊會把正常的公司消息一起擋掉。
FINANCE_CONTEXT = re.compile(
    r"股價|股票|股東|股利|股份|營收|財報|財測|法說|董事會|目標價|評等"
    r"|外資|投信|自營|法人|分析師|投顧|券商|市值|籌碼|融資|融券"
    r"|漲|跌|開盤|收盤|盤中|盤後|掛牌|上市|上櫃|興櫃|除息|除權|增資|減資"
    r"|訂單|接單|出貨|產能|擴廠|建廠|毛利|營益|EPS|每股|季報|月營收"
    r"|併購|收購|入股|轉投資|報價|供應鏈|客戶|代工|營運|獲利|虧損"
    r"|處分|取得|資產|廠房|設備|業外|認列|收益|盈餘|配息|配股|庫藏|買回"
    r"|標案|得標|合約|簽約|投資|資本支出|稼動|良率|漲價|降價|庫存|拉貨|砍單"
    r"|營業|業績|銷售|出口|接獲|擴產|量產|投產|新廠|子公司|集團|上下游"
)

_CODE4 = re.compile(r"\d{4}")


def load_ambiguous(path="ambiguous_alias.json"):
    """載入「剛好是常用詞」的公司簡稱清單。"""
    try:
        with open(path, encoding="utf-8") as f:
            return set(json.load(f).get("ambiguous", []))
    except (FileNotFoundError, json.JSONDecodeError, AttributeError, TypeError):
        return set()


def longest_alias_hit(title: str, sid: str, linker: Linker) -> str:
    """這檔是靠哪個別名被比中的。link.py 不回傳這個資訊，這裡重新推一次。"""
    best = ""
    for a in linker.aliases.get(sid, []):
        if a in title and len(a) > len(best):
            best = a
    return best


def confirm_stocks(title, stocks, linker, ambiguous):
    """剔除疑似誤配的個股，回傳 (通過的個股, 被剔除的別名)。

    地雷別名（統一、全新、可成、南亞…）必須有旁證才算數：
    標題出現該檔股號，或標題有財經語境字眼。

    為什麼不是直接把這些別名從別名表拿掉：那樣「統一8月營收創高」
    也會一起失效，而那是我們真正要的東西。問題不在別名本身，
    在於缺少判斷它是不是在講公司的依據。
    """
    kept, rejected = [], []
    for sid, rel in stocks:
        alias = longest_alias_hit(title, sid, linker)
        if alias not in ambiguous:
            kept.append((sid, rel))
            continue
        if sid in _CODE4.findall(title) or FINANCE_CONTEXT.search(title):
            kept.append((sid, rel))
        else:
            rejected.append(alias)

    # 主角被剔除時，讓剩下的第一檔遞補成 primary
    if kept and kept[0][1] != "primary":
        kept[0] = (kept[0][0], "primary")
    return kept, rejected


# ---------------------------------------------------------------- 對股

_DOWNGRADE = {"primary": "direct", "direct": "indirect", "indirect": "indirect"}


def link_stocks(linker: Linker, title: str, desc: str, use_desc: bool):
    """先用標題對股；標題對不到才退而用摘要，且把關聯度降一級。

    為什麼要降級：摘要提到某檔，不代表那則新聞是在講它。
    「台積電法說會」的內文順帶提到聯電，聯電不該被當成主角。
    降級後它仍然會出現在該檔的版面上，但不會搶走 primary。

    為什麼還是要用摘要：Yahoo 這類聚合來源的標題常常寫得很文學，
    例如「護國神山地位動搖？」整句沒有任何公司名。全靠標題會丟掉不少有效訊息。
    摘要文字只存在於記憶體，不入庫。
    """
    hits = linker.link(title)
    if hits:
        return hits, False
    if use_desc and desc:
        hits = linker.link(desc[:300])
        if hits:
            return [(sid, _DOWNGRADE[rel]) for sid, rel in hits], True
    return [], False


# ---------------------------------------------------------------- 來源狀態

def load_state(conn, source_id):
    row = conn.execute(
        """SELECT etag, last_modified, last_attempt_at, last_success_at,
                  consecutive_failures
           FROM feed_state WHERE source_id = ?""",
        (source_id,),
    ).fetchone()
    if not row:
        return {"etag": None, "last_modified": None, "last_attempt_at": None,
                "last_success_at": None, "consecutive_failures": 0}
    return dict(zip(
        ["etag", "last_modified", "last_attempt_at", "last_success_at",
         "consecutive_failures"], row))


def save_state(conn, source_id, **kw):
    conn.execute(
        "INSERT OR IGNORE INTO feed_state (source_id) VALUES (?)", (source_id,))
    cols = ", ".join(f"{k} = ?" for k in kw)
    conn.execute(
        f"UPDATE feed_state SET {cols} WHERE source_id = ?",
        (*kw.values(), source_id),
    )
    conn.commit()


# ---------------------------------------------------------------- 抓取單一來源

def entry_time(entry, fallback: datetime) -> datetime:
    """feedparser 已經把 RFC822 / ISO 等各種日期格式統一成 UTC 的 struct_time。

    自己剖析這些格式是純粹的苦工，而且台灣媒體常常混用時區寫法，
    所以這支程式依賴 feedparser 而不是 xml.etree。
    """
    for key in ("published_parsed", "updated_parsed"):
        st = entry.get(key)
        if st:
            try:
                return datetime(*st[:6], tzinfo=timezone.utc)
            except (TypeError, ValueError):
                pass
    return fallback


def fetch_source(conn, src, force=False, timeout=30):
    """回傳 (entries, 說明字串)。entries 為 None 代表這次不需要處理。"""
    sid = src["id"]
    now = datetime.now(timezone.utc)
    state = load_state(conn, sid)

    # 最小間隔：手動連按 Run workflow 時不要重複打對方伺服器
    gap = src.get("min_interval_minutes", 20)
    last_attempt = state["last_attempt_at"]
    if not force and last_attempt:
        try:
            elapsed = (now - datetime.fromisoformat(last_attempt)).total_seconds() / 60
            if elapsed < gap:
                return None, f"距上次 {elapsed:.0f} 分鐘，未達 {gap} 分鐘間隔，略過"
        except ValueError:
            pass

    # 條件式請求。伺服器回 304 就完全不用下載也不用剖析，
    # 這是控制請求成本最有效的一招，之後擴到幾十個 feed 時差別更明顯。
    headers = {
        "Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.8",
        "Accept-Language": "zh-TW,zh;q=0.9",
        # netutil 預設的 UA 是 tw-stock-pipeline/0.2。交易所的 OpenAPI 不在意，
        # 但不少新聞網站會擋掉看起來不像瀏覽器的 UA，直接回 403。
        # 這裡改送一個常見的 UA，同時在括號裡註明用途與聯絡方式，
        # 讓對方站方看得出這是誰、要封鎖也找得到人，不是偽裝成一般使用者。
        "User-Agent": src.get("user_agent", DEFAULT_UA),
    }
    if state["etag"]:
        headers["If-None-Match"] = state["etag"]
    if state["last_modified"]:
        headers["If-Modified-Since"] = state["last_modified"]

    save_state(conn, sid, last_attempt_at=now.isoformat())

    try:
        status, content, resp_headers = fetch_bytes(
            src["url"], extra_headers=headers, timeout=timeout)
    except Exception as e:
        save_state(conn, sid,
                   last_status=f"{type(e).__name__}: {str(e)[:120]}",
                   consecutive_failures=state["consecutive_failures"] + 1)
        return None, f"失敗 {type(e).__name__}: {str(e)[:100]}"

    if status == 304:
        save_state(conn, sid, last_status="304 內容未變",
                   last_success_at=now.isoformat(), consecutive_failures=0)
        return None, "304 內容未變"

    feed = feedparser.parse(content)
    entries = feed.entries or []

    # feedparser 的 bozo 旗標代表 XML 有瑕疵。它通常還是能剖析出東西，
    # 所以不當成失敗，只在有東西的時候放行、沒東西才記為異常。
    if not entries:
        note = "剖析後 0 筆"
        if getattr(feed, "bozo", 0):
            note += f"（XML 異常：{str(getattr(feed, 'bozo_exception', ''))[:60]}）"
        save_state(conn, sid, last_status=note,
                   consecutive_failures=state["consecutive_failures"] + 1)
        return [], note

    save_state(conn, sid,
               etag=resp_headers.get("ETag"),
               last_modified=resp_headers.get("Last-Modified"),
               last_success_at=now.isoformat(),
               last_status=f"HTTP {status}，{len(entries)} 筆",
               consecutive_failures=0,
               last_item_count=len(entries))
    return entries, f"HTTP {status}，{len(entries)} 筆"


# ---------------------------------------------------------------- 寫入

def ingest_entries(conn, linker, src, entries, *, use_desc=False,
                   max_age_days=7, audit=None, ambiguous=None,
                   mismatch_log=None):
    """把一個來源的項目寫進資料庫。回傳統計 dict。

    抽成獨立函式是為了可測試：不需要網路就能餵假資料進來驗證聚合行為。
    """
    audit = audit if audit is not None else []
    ambiguous = ambiguous if ambiguous is not None else set()
    mismatch_log = mismatch_log if mismatch_log is not None else []
    now = datetime.now(timezone.utc)
    oldest = now - timedelta(days=max_age_days)
    tier = int(src.get("tier", 3))
    sid = src["id"]

    st = {"new": 0, "merged": 0, "grey": 0, "blocked": 0, "market": 0,
          "mismatch": 0, "dup": 0, "retitled": 0, "unmatched": 0, "stale": 0}

    for e in entries:
        title = (e.get("title") or "").strip()
        link = (e.get("link") or "").strip()
        if not title or not link:
            continue

        published = entry_time(e, now)
        if published < oldest:
            st["stale"] += 1
            continue
        # 有些來源的時間會超前，夾回現在，否則熱度的時間衰減會算成負的
        if published > now + timedelta(hours=6):
            published = now

        uh = url_hash(link)
        row = conn.execute(
            "SELECT article_id, title FROM article WHERE url_hash = ?", (uh,)
        ).fetchone()
        if row:
            # 網址相同但標題變了：媒體改標，屬於更新不是新增。
            # 只更新 article 的標題，不動 event.headline，
            # 否則版面上的說法會在一天內反覆跳動。
            if row[1] != title:
                conn.execute("UPDATE article SET title = ? WHERE article_id = ?",
                             (title, row[0]))
                st["retitled"] += 1
            else:
                st["dup"] += 1
            continue

        desc = (e.get("summary") or e.get("description") or "")
        desc = re.sub(r"<[^>]+>", " ", desc)  # RSS 的摘要常夾 HTML
        stocks, from_desc = link_stocks(linker, title, desc, use_desc)
        if not stocks:
            st["unmatched"] += 1
            continue

        stocks, rejected = confirm_stocks(title, stocks, linker, ambiguous)
        if not stocks:
            st["mismatch"] += 1
            mismatch_log.append((rejected[0] if rejected else "", title))
            continue

        primary = stocks[0][0]

        # 大盤行情播報不建事件。放在對股之後才判斷，是因為要知道主角是誰
        # 才能檢查它在標題裡是不是只以 ADR 或期貨的形式出現。
        if is_market_noise(title, primary, linker) or is_offtopic(title):
            st["market"] += 1
            continue

        etype = classify_media(title)
        unverified = linker.is_unverified(title)

        ev_id, verdict, sim, cand = aggregate.match_event(
            conn, title, primary, published)

        if ev_id is not None:
            aggregate.merge_into_event(
                conn, ev_id, headline=title, stocks=stocks, tier=tier,
                published_at=published.isoformat(), is_unverified=unverified)
            st["merged"] += 1
        else:
            ev_id = aggregate.create_event(
                conn, headline=title, event_type=etype, stocks=stocks,
                tier=tier, published_at=published.isoformat(),
                is_unverified=unverified)
            if verdict == "grey":
                st["grey"] += 1
            elif verdict.startswith("new(擋下"):
                st["blocked"] += 1
            else:
                st["new"] += 1

        conn.execute(
            """INSERT INTO article (source_id, source_tier, url_hash, url,
                                    title, summary, published_at, fetched_at,
                                    event_id)
               VALUES (?,?,?,?,?,NULL,?,?,?)""",
            (sid, tier, uh, clean_url(link), title,
             published.isoformat(), now.isoformat(), ev_id),
        )

        # 只要有找到候選就記錄，包含判成新事件的。
        # 上一輪只記 merge / grey / blocked，結果稽核檔只有 1 組，
        # 完全看不出「差一點就該併」的配對有多少，等於無法判斷門檻高低。
        if cand:
            conn.execute(
                """INSERT INTO audit_log (run_at, source_id, verdict, sim,
                                          stock_id, new_title, cand_title, from_desc)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (now.isoformat(), sid, verdict, sim, primary,
                 title, cand, int(from_desc)),
            )
            audit.append(1)  # 只用來算數量，內容以資料庫為準

    conn.commit()
    save_state(conn, sid, last_new_count=st["new"] + st["merged"])
    return st


# ---------------------------------------------------------------- 稽核報表

def write_audit(conn, path="cluster_audit.md", days=3, max_rows=60):
    """把最近幾天的聚合判斷寫成一份可讀的稽核檔。

    兩個修正（2026-09-12）：

    1. 原本最後一區設了「相似度 ≥ 0.15 才顯示」的條件，低於 0.15 的配對
       被記進統計數字卻不進任何區塊，於是出現「日誌說 12 組、檔案顯示 0 組」
       這種自相矛盾的結果。現在最後多一個收容區，保證每一組判斷都看得到。
    2. 原本只呈現當次執行的結果，整份覆寫。排程一天跑四次，前三班的判斷
       會被蓋掉，而門檻校準恰好需要累積樣本。現在改成讀資料庫裡最近 N 天的紀錄。
    """
    now = datetime.now(TPE)
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows = conn.execute(
        """SELECT verdict, sim, stock_id, new_title, cand_title, source_id,
                  from_desc, run_at
           FROM audit_log WHERE run_at >= ? ORDER BY sim DESC""",
        (since,),
    ).fetchall()

    lines = [
        "# 事件聚合稽核",
        "",
        f"產生時間：{now:%Y-%m-%d %H:%M}（台北）　"
        f"涵蓋區間：最近 {days} 天　判斷 {len(rows)} 組",
        "",
        f"門檻：合併 ≥ {aggregate.SIM_MERGE}　灰帶 ≥ {aggregate.SIM_GREY}　"
        f"{aggregate.NGRAM}-gram　時間窗 {aggregate.WINDOW_HOURS} 小時",
        "",
        "只列出「有找到對照組」的判斷。完全找不到候選事件的新聞不會出現在這裡。",
        "",
    ]

    shown = set()

    def section(title, pred, hint):
        items = [r for i, r in enumerate(rows) if i not in shown and pred(r)]
        for i, r in enumerate(rows):
            if i not in shown and pred(r):
                shown.add(i)
        out = [f"## {title}　{len(items)} 組", "", hint, ""]
        if not items:
            return out + ["（無）", ""]
        out += ["| 相似度 | 個股 | 新進標題 | 對照的既有事件 |", "|---|---|---|---|"]
        for verdict, sim, stock, new, cand, src, fd, _ts in items[:max_rows]:
            mark = "（靠摘要對股）" if fd else ""
            out.append(
                f"| {sim:.3f} | {stock} | "
                f"{(new or '').replace('|', '｜')}{mark} | "
                f"{(cand or '').replace('|', '｜')} |")
        if len(items) > max_rows:
            out.append(f"| … | | 另有 {len(items) - max_rows} 組未列出 | |")
        return out + [""]

    lines += section(
        "合併", lambda r: r[0].startswith("merge"),
        "分數低於 0.55 的要特別看，那是誤併最可能發生的區間。")
    lines += section(
        "灰帶（目前當新事件處理）", lambda r: r[0] == "grey",
        "如果這區大量都是同一件事，代表合併門檻設太高，把 --sim-merge 調低。")
    lines += section(
        "被守門規則擋下", lambda r: r[0].startswith("new(擋下"),
        "分數很高卻被擋，通常是對的（例如調升與調降）。"
        "若發現誤擋，改 aggregate.py 的 _OPPOSITES。")
    lines += section(
        "差一點（0.15 以上但未達灰帶）", lambda r: (r[1] or 0) >= 0.15,
        "這一區是判斷門檻高低的主要依據。若裡面大量是同一件事，"
        "代表灰帶下緣 --sim-grey 設太高。")
    lines += section(
        "其餘（相似度低於 0.15）", lambda r: True,
        "分數低到這種程度，通常代表兩則標題幾乎沒有共同字詞。"
        "若這區裡仍有明顯是同一件事的配對，代表字元相似度對中文標題的鑑別力不足，"
        "該走 LLM 判斷而不是繼續調數字。")

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return len(rows)


# ---------------------------------------------------------------- 主流程

def health_check(conn, sources):
    """來源健康檢查。台灣媒體的 RSS 會無預警下架，要能及早發現。"""
    now = datetime.now(timezone.utc)
    warnings = []
    for src in sources:
        row = conn.execute(
            """SELECT last_success_at, consecutive_failures, last_status
               FROM feed_state WHERE source_id = ?""", (src["id"],)).fetchone()
        if not row or not row[0]:
            warnings.append(f"⚠ {src['name']}（{src['id']}）從未成功抓取過")
            continue
        try:
            hours = (now - datetime.fromisoformat(row[0])).total_seconds() / 3600
        except ValueError:
            continue
        if hours > 24:
            warnings.append(
                f"⚠ {src['name']}（{src['id']}）已 {hours:.0f} 小時沒有成功，"
                f"最後狀態：{row[2]}")
    return warnings


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="news.db")
    ap.add_argument("--aliases", default="aliases.json")
    ap.add_argument("--sources", default="rss_sources.json")
    ap.add_argument("--only", help="只抓指定的 source id")
    ap.add_argument("--force", action="store_true", help="忽略最小間隔")
    ap.add_argument("--use-desc", action="store_true",
                    help="標題對不到股時，改用摘要輔助對股。"
                         "預設關閉：2026-09-12 實測精確度太差，"
                         "撿回來的是「巴西力爭東協完整夥伴」這類完全無關的新聞")
    ap.add_argument("--max-age-days", type=int, default=7)
    ap.add_argument("--ambiguous", default="ambiguous_alias.json")
    ap.add_argument("--audit", default="cluster_audit.md")
    ap.add_argument("--audit-days", type=int, default=3,
                    help="稽核檔涵蓋最近幾天的判斷。紀錄累積在資料庫，不會被覆寫")
    ap.add_argument("--sim-merge", type=float)
    ap.add_argument("--sim-grey", type=float)
    ap.add_argument("--ngram", type=int)
    args = ap.parse_args()

    # 門檻用參數覆寫，這樣調參數不必改程式碼
    if args.sim_merge is not None:
        aggregate.SIM_MERGE = args.sim_merge
    if args.sim_grey is not None:
        aggregate.SIM_GREY = args.sim_grey
    if args.ngram is not None:
        aggregate.NGRAM = args.ngram

    try:
        with open(args.sources, encoding="utf-8") as f:
            cfg = json.load(f)
    except FileNotFoundError:
        print(f"找不到 {args.sources}", file=sys.stderr)
        sys.exit(1)

    sources = [s for s in cfg.get("sources", []) if s.get("enabled")]
    if args.only:
        sources = [s for s in sources if s["id"] == args.only]
    if not sources:
        print("沒有啟用中的來源，請檢查 rss_sources.json 的 enabled 欄位")
        return

    try:
        linker = Linker(args.aliases)
    except FileNotFoundError:
        print(f"找不到 {args.aliases}，請先執行 build_universe.py", file=sys.stderr)
        sys.exit(1)

    conn = open_db(args.db)
    aggregate.ensure_schema(conn)

    ambiguous = load_ambiguous(args.ambiguous)
    if not ambiguous:
        print(f"（找不到 {args.ambiguous}，誤配確認未啟用）", file=sys.stderr)

    audit, totals, mismatch_log = [], {}, []
    print(f"門檻：合併 ≥ {aggregate.SIM_MERGE}　灰帶 ≥ {aggregate.SIM_GREY}"
          f"　{aggregate.NGRAM}-gram\n")

    for src in sources:
        print(f"[{src['id']}] {src['name']}  tier {src.get('tier')}")
        entries, note = fetch_source(conn, src, force=args.force)
        print(f"  {note}")
        if not entries:
            continue
        st = ingest_entries(conn, linker, src, entries,
                            use_desc=args.use_desc,
                            max_age_days=args.max_age_days,
                            audit=audit, ambiguous=ambiguous,
                            mismatch_log=mismatch_log)
        print("  新事件 {new}　併入 {merged}　灰帶 {grey}　守門擋下 {blocked}　"
              "大盤噪音 {market}　疑似誤配 {mismatch}　重複 {dup}　改標 {retitled}　"
              "不在名單 {unmatched}".format(**st))
        for k, v in st.items():
            totals[k] = totals.get(k, 0) + v

    total_audit = write_audit(conn, args.audit, days=args.audit_days)

    print("\n=== 合計 ===")
    if totals:
        print("新事件 {new}　併入既有 {merged}　灰帶 {grey}　守門擋下 {blocked}　"
              "大盤噪音 {market}　疑似誤配 {mismatch}　重複 {dup}　改標 {retitled}　"
              "不在名單 {unmatched}".format(**totals))
    print(f"稽核檔：{args.audit}　本次 {len(audit)} 組判斷，"
          f"檔案涵蓋最近 {args.audit_days} 天共 {total_audit} 組")

    # 印出被誤配確認擋下的標題，方便判斷清單要不要補
    if mismatch_log:
        print(f"\n疑似誤配、已丟棄（{len(mismatch_log)} 則，最多列 8 則）：")
        for alias, t in mismatch_log[:8]:
            print(f"  「{alias}」 ← {t[:52]}")
        print("  若其中有該留下的，或發現還有漏網的，"
              "改 ambiguous_alias.json 的 ambiguous 陣列")

    for w in health_check(conn, cfg.get("sources", [])):
        print(w, file=sys.stderr)


if __name__ == "__main__":
    main()
