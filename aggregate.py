"""
aggregate.py — 事件聚合

一件事十家媒體報，要存十筆 article、一筆 event。這支檔案負責判斷
「這則新聞是既有事件的第 N 家報導，還是一件新的事」。

流程（對應交接文件的 Stage B / C / D）：

    B 候選集   只跟「同一 primary_stock_id 且 36 小時內」的既有事件比對。
               全庫兩兩比是 O(n²)，加上這兩個條件後每次比對通常不到 10 筆。

    C 相似度   正規化標題的字元 n-gram Jaccard，外加兩道守門規則。
               >= SIM_MERGE  併入
               >= SIM_GREY   灰帶，留給 LLM 判斷（目前先當作新事件並記錄下來）
               <  SIM_GREY   新事件

    D 更新     source_count +1、last_seen_at 取較晚、best_tier 取較權威。
               headline 不覆寫，除非併入的來源 tier 更高
               （原本是媒體傳聞，後來 MOPS 正式公告，就該換成公告的說法）。

關於門檻數字：交接文件原訂 3-gram、0.62 / 0.45。我拿標註過的真實標題測過，
3-gram Jaccard 在中文改寫標題上只有 0.24 到 0.27，0.62 幾乎不可能達到，
等於聚合完全不會發生。改成 2-gram 並把門檻降到 0.45 / 0.28。
這組數字仍然是推測值，請跑過幾天真實資料、看 cluster_audit.md 再微調。
"""

import math
import re
import sqlite3
from datetime import datetime, timedelta, timezone

from textnorm import char_ngrams, jaccard, normalize_title

# ---------------------------------------------------------------- 可調參數

NGRAM = 2            # 中文標題用 2-gram，3-gram 對改寫過的標題太嚴
SIM_MERGE = 0.45     # 以上直接併入
SIM_GREY = 0.28      # 介於兩者之間 = 灰帶
WINDOW_HOURS = 36    # 候選事件的時間窗

TIER_WEIGHT = {1: 1.0, 2: 0.8, 3: 0.5, 4: 0.2}
RECENCY_TAU_HOURS = 18.0


# ---------------------------------------------------------------- 資料表擴充

# 不動 mops_fetch.py 的 SCHEMA，額外補這些。全部 IF NOT EXISTS，重複執行安全。
SCHEMA_EXTRA = """
CREATE TABLE IF NOT EXISTS feed_state (
    source_id            TEXT PRIMARY KEY,
    etag                 TEXT,
    last_modified        TEXT,
    last_attempt_at      TEXT,
    last_success_at      TEXT,
    last_status          TEXT,
    consecutive_failures INTEGER DEFAULT 0,
    last_item_count      INTEGER DEFAULT 0,
    last_new_count       INTEGER DEFAULT 0
);

-- Stage B 的候選集查詢靠這個索引，沒有它每次都會全表掃描
CREATE INDEX IF NOT EXISTS idx_event_primary
    ON event(primary_stock_id, first_seen_at);
CREATE INDEX IF NOT EXISTS idx_article_event ON article(event_id);
CREATE INDEX IF NOT EXISTS idx_article_source ON article(source_id);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_EXTRA)
    conn.commit()


# ---------------------------------------------------------------- 守門規則

# 反向詞。字元相似度分不出「調升」與「調降」，但這兩則絕對不是同一件事。
# 實測中最容易誤併的一對就是目標價調升與調降，相似度比許多真正該併的還高。
_OPPOSITES = [
    (r"調升|上修|上調|調高", r"調降|下修|下調|調低"),
    (r"買超|買進|加碼|回補", r"賣超|賣出|減碼|調節"),
    (r"漲停|大漲|上漲|走揚|攀高|飆", r"跌停|大跌|下跌|走低|重挫|摔"),
    (r"看多|樂觀|看好", r"看空|保守|看壞|示警"),
    (r"增資|擴產|擴廠|加碼投資", r"減資|減產|關廠|撤資"),
    (r"獲利|轉盈|成長|創高", r"虧損|轉虧|衰退|探底"),
    (r"通過|核准|同意", r"否決|駁回|撤銷|終止"),
    (r"新增|增加|提高", r"取消|刪減|降低"),
]
_OPPOSITES = [(re.compile(a), re.compile(b)) for a, b in _OPPOSITES]

_NUM = re.compile(r"\d+(?:\.\d+)?")


def conflict(norm_a: str, norm_b: str) -> str:
    """回傳擋下合併的理由，沒有理由則回傳空字串。

    兩道規則都是「寧可少併，不要亂併」。誤併兩件相反的事會讓版面直接說錯話，
    漏併只是同一件事出現兩列，代價小得多。
    """
    for pat_up, pat_down in _OPPOSITES:
        if (pat_up.search(norm_a) and pat_down.search(norm_b)) or \
           (pat_down.search(norm_a) and pat_up.search(norm_b)):
            return "反向詞"

    # 數字守門：兩邊都有數字卻完全不重疊，多半是不同的價位、季度或金額。
    # 只有一邊有數字時不擋，因為那通常只是一家寫得比較細。
    na, nb = set(_NUM.findall(norm_a)), set(_NUM.findall(norm_b))
    if na and nb and not (na & nb):
        return "數字不交集"

    return ""


# ---------------------------------------------------------------- 灰帶處理

def llm_same_event(title_a: str, title_b: str) -> bool | None:
    """灰帶的 LLM 二元判斷。目前是佔位函式，永遠回傳 None（= 交給演算法當新事件）。

    要接的時候在這裡呼叫 Sonnet，prompt 大意是
    「以下兩個新聞標題是否在講同一件事，只回答 是 或 否」。
    量少（一天幾十次），成本可控。
    在那之前，所有灰帶配對都會寫進 cluster_audit.md，
    請先用那份檔案觀察誤判率，確定門檻站得住腳再花錢。
    """
    return None


# ---------------------------------------------------------------- 主要邏輯

def _parse_iso(ts: str) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def find_candidates(conn, stock_id: str, when: datetime):
    """Stage B：同一主角、時間窗內、仍然活著的事件。"""
    cutoff = (when - timedelta(hours=WINDOW_HOURS)).isoformat()
    ceiling = (when + timedelta(hours=WINDOW_HOURS)).isoformat()
    return conn.execute(
        """SELECT event_id, headline, best_tier, first_seen_at, last_seen_at,
                  source_count, is_unverified
           FROM event
           WHERE primary_stock_id = ?
             AND status = 'active'
             AND first_seen_at >= ? AND first_seen_at <= ?""",
        (stock_id, cutoff, ceiling),
    ).fetchall()


def match_event(conn, title: str, stock_id: str, when: datetime):
    """回傳 (event_id 或 None, 判定, 相似度, 對照標題)。

    判定：merge / merge(LLM) / grey / new / new(擋下:理由)

    2026-09-12 修正兩個錯誤：

    1. 原本只挑相似度最高的候選做守門檢查，於是相似度 0.05、本來就該判新事件
       的配對，只要撞到反向詞或數字不交集也會被記成「擋下」，稽核檔上出現一堆
       假的擋下紀錄。現在只有分數有進灰帶的候選才做守門檢查。
    2. 最高分的候選被擋下之後，原本直接宣告新事件。正確做法是退而比對次高的
       候選，因為被擋下只代表「那一則不是它」，不代表沒有別的事件是它。
    """
    norm = normalize_title(title)
    grams = char_ngrams(norm, NGRAM)

    scored = []
    for ev_id, headline, *_ in find_candidates(conn, stock_id, when):
        cand_norm = normalize_title(headline)
        scored.append((jaccard(grams, char_ngrams(cand_norm, NGRAM)),
                       ev_id, headline, cand_norm))
    if not scored:
        return None, "new", 0.0, ""

    scored.sort(key=lambda x: -x[0])
    top_sim, _, top_headline, _ = scored[0]
    blocked = None

    for sim, ev_id, headline, cand_norm in scored:
        if sim < SIM_GREY:
            break  # 已排序，後面只會更低

        reason = conflict(norm, cand_norm)
        if reason:
            # 記下第一個被擋的，繼續看次高的候選
            if blocked is None:
                blocked = (reason, sim, headline)
            continue

        if sim >= SIM_MERGE:
            return ev_id, "merge", sim, headline

        verdict = llm_same_event(title, headline)
        if verdict is True:
            return ev_id, "merge(LLM)", sim, headline
        return None, "grey", sim, headline

    if blocked:
        reason, sim, headline = blocked
        return None, f"new(擋下:{reason})", sim, headline
    return None, "new", top_sim, top_headline


def create_event(conn, *, headline, event_type, stocks, tier,
                 published_at, is_unverified) -> int:
    cur = conn.execute(
        """INSERT INTO event (headline, event_type, is_unverified,
                              primary_stock_id, first_seen_at, last_seen_at,
                              source_count, best_tier, status)
           VALUES (?,?,?,?,?,?,1,?,'active')""",
        (headline, event_type, int(is_unverified), stocks[0][0],
         published_at, published_at, tier),
    )
    event_id = cur.lastrowid
    conn.executemany(
        "INSERT OR IGNORE INTO event_stock VALUES (?,?,?)",
        [(event_id, sid, rel) for sid, rel in stocks],
    )
    return event_id


def merge_into_event(conn, event_id: int, *, headline, stocks, tier,
                     published_at, is_unverified) -> None:
    """Stage D。"""
    row = conn.execute(
        """SELECT best_tier, first_seen_at, last_seen_at
           FROM event WHERE event_id = ?""",
        (event_id,),
    ).fetchone()
    if not row:
        return
    old_tier, first_seen, last_seen = row

    new_tier = min(old_tier, tier)
    # 較晚的時間當 last_seen，較早的當 first_seen。
    # 後者會發生在補抓舊文章的時候，不處理的話熱度衰減會算錯基準。
    new_last = max(last_seen or published_at, published_at)
    new_first = min(first_seen or published_at, published_at)

    if tier < old_tier:
        # 更權威的來源進來了，換成它的說法。
        # 典型情境：先有「傳鴻海獲追加訂單」，後有 MOPS 正式公告。
        conn.execute(
            """UPDATE event
               SET headline = ?, is_unverified = ?, best_tier = ?,
                   first_seen_at = ?, last_seen_at = ?,
                   source_count = source_count + 1
               WHERE event_id = ?""",
            (headline, int(is_unverified), new_tier, new_first, new_last, event_id),
        )
    else:
        conn.execute(
            """UPDATE event
               SET best_tier = ?, first_seen_at = ?, last_seen_at = ?,
                   source_count = source_count + 1
               WHERE event_id = ?""",
            (new_tier, new_first, new_last, event_id),
        )

    # 新來源可能提到原本沒關聯到的個股，補進去但不改既有 relevance
    conn.executemany(
        "INSERT OR IGNORE INTO event_stock VALUES (?,?,?)",
        [(event_id, sid, rel) for sid, rel in stocks],
    )


# ---------------------------------------------------------------- 熱度

def heat(source_count: int, best_tier: int, first_seen_at: str,
         now: datetime | None = None) -> float:
    """log(1+來源數) × 來源權重 × 時間衰減。

    log 而非線性：第 2 家跟第 1 家的差距，比第 12 家跟第 11 家有意義得多。
    """
    now = now or datetime.now(timezone.utc)
    seen = _parse_iso(first_seen_at) or now
    hours = max((now - seen).total_seconds() / 3600.0, 0.0)
    recency = math.exp(-hours / RECENCY_TAU_HOURS)
    return math.log(1 + max(source_count, 0)) * TIER_WEIGHT.get(best_tier, 0.2) * recency
