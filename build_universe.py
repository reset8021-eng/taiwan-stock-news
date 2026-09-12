"""
build_universe.py — 產生台股前 150 大市值名單與別名對照表

資料來源皆為官方免費 OpenAPI：
  TWSE  https://openapi.twse.com.tw/v1/...
  TPEx  https://www.tpex.org.tw/openapi/v1/...

輸出：
  universe.csv        當日前 150 名單（含市值、排名、產業）
  aliases.json        別名對照表，供 entity linking 使用
  collisions.txt      別名衝突報告，需要人工處理的部分

用法：
  python build_universe.py --top 150 --seed alias_seed.json
"""

import argparse
import csv
import json
import re
import ssl
import sys
from datetime import datetime, timezone, timedelta

from netutil import fetch_json

TPE = timezone(timedelta(hours=8))

TWSE_PROFILE = "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"
TWSE_DAILY = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
TPEX_PROFILE = "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O"
TPEX_DAILY = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes"

HEADERS = {"User-Agent": "universe-builder/0.1"}

# 面額非 10 元的個股，股數不能用「資本額 / 10」推算，必須手動維護
PAR_VALUE_OVERRIDE = {
    # "1234": 5.0,
}

# 明確排除：ETF、受益證券、存託憑證、特別股
EXCLUDE_PATTERN = re.compile(r"^(00|91|92|93|94|95|96|97|98)")

NOISE_WORDS = [
    "股份有限公司", "控股股份有限公司", "股份公司", "有限公司",
    "投資控股", "金融控股", "控股", "工業", "企業", "實業",
    "科技", "電子", "光電", "半導體", "生技", "製藥",
]


def to_float(x):
    if x is None:
        return None
    s = str(x).replace(",", "").strip()
    if s in ("", "-", "--", "null"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def pick(row, *keys):
    for k in keys:
        if k in row and row[k] not in (None, ""):
            return row[k]
    return None


def load_market(profile_url, daily_url, market_label, allow_insecure=False):
    """回傳 {stock_id: {...}}，合併基本資料與當日收盤。"""
    profiles = {}
    profile_data, _ = fetch_json(profile_url, allow_insecure)
    for row in profile_data:
        sid = pick(row, "公司代號", "SecuritiesCompanyCode")
        if not sid or EXCLUDE_PATTERN.match(sid):
            continue
        capital = to_float(pick(row, "實收資本額", "已發行普通股數或TDR原發行股數"))
        profiles[sid] = {
            "stock_id": sid,
            "market": market_label,
            "name_full": pick(row, "公司名稱", "CompanyName") or "",
            "name_short": pick(row, "公司簡稱", "CompanyAbbreviation") or "",
            "name_en": pick(row, "英文簡稱", "英文公司名稱") or "",
            "industry": pick(row, "產業別", "IndustryCode") or "",
            "capital": capital,
        }

    daily_data, _ = fetch_json(daily_url, allow_insecure)
    for row in daily_data:
        sid = pick(row, "Code", "SecuritiesCompanyCode", "證券代號")
        if sid not in profiles:
            continue
        close = to_float(pick(row, "ClosingPrice", "Close", "收盤價"))
        profiles[sid]["close"] = close

    return profiles


def compute_market_cap(rec):
    """市值 = 收盤價 x 發行股數；股數由實收資本額 / 面額推得。"""
    close = rec.get("close")
    capital = rec.get("capital")
    if not close or not capital:
        return None
    par = PAR_VALUE_OVERRIDE.get(rec["stock_id"], 10.0)
    shares = capital / par
    return close * shares


def strip_noise(name):
    out = name
    for w in NOISE_WORDS:
        out = out.replace(w, "")
    out = re.sub(r"[-－]?KY$", "", out.strip())
    return out.strip()


# 真正撞名、無法靠長度規則排除的別名，直接封殺
ALIAS_BLOCKLIST = {"EMC", "CSC", "TC", "RT", "FE", "WT", "ACC", "PEC", "MIC", "PCC", "FPC"}

LATIN_RE = re.compile(r"^[A-Za-z0-9 .&\-]+$")


def generate_aliases(rec, seed, reserved_shorts):
    """
    自動候選 + 手動 seed 合併，並套用兩條過濾規則：
      1. 去雜訊詞產生的「弱別名」若等於別家官方簡稱，丟棄
         （南亞科技 -> 南亞 會撞到 1303，聯華電子 -> 聯華 會撞到 1229）
      2. 長度小於 5 的純英文縮寫丟棄，除非是手動 seed
         （TC、RT、FE、CSC 這種在中文新聞裡不會出現，只會製造誤判）
    """
    sid = rec["stock_id"]
    manual = set(seed.get(sid, []))
    cand = set(manual)

    if rec["name_short"]:
        cand.add(rec["name_short"].strip())
    if rec["name_full"]:
        cand.add(rec["name_full"].strip())
        stripped = strip_noise(rec["name_full"])
        if len(stripped) >= 2 and reserved_shorts.get(stripped, sid) == sid:
            cand.add(stripped)
    if rec["name_en"]:
        cand.add(rec["name_en"].strip())

    out = set()
    for a in cand:
        if len(a) < 2 or a.upper() in ALIAS_BLOCKLIST:
            continue
        if a in manual:
            out.add(a)
            continue
        if LATIN_RE.match(a) and len(a.replace(" ", "")) < 5:
            continue
        out.add(a)

    return sorted(out, key=lambda s: (-len(s), s))


def find_collisions(alias_map):
    """
    兩種衝突：
      exact  — 同一個字串指向兩檔以上，必須人工處理
      substr — A 是 B 的子字串且屬於不同公司，比對時必須長字串優先
    """
    owner = {}
    exact, substr = [], []

    for sid, aliases in alias_map.items():
        for a in aliases:
            owner.setdefault(a, []).append(sid)

    for a, sids in owner.items():
        if len(sids) > 1:
            exact.append((a, sids))

    keys = sorted(owner.keys(), key=len)
    for i, short in enumerate(keys):
        for long in keys[i + 1:]:
            if short != long and short in long:
                if set(owner[short]) != set(owner[long]):
                    substr.append((short, owner[short], long, owner[long]))

    return exact, substr


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=150)
    ap.add_argument("--seed", default="alias_seed.json")
    ap.add_argument("--outdir", default=".")
    ap.add_argument("--no-insecure-tpex", dest="allow_insecure_tpex",
                    action="store_false", default=True,
                    help="不允許對櫃買降級連線，櫃買抓不到就跳過")
    args = ap.parse_args()

    try:
        with open(args.seed, encoding="utf-8") as f:
            seed = json.load(f)
    except FileNotFoundError:
        print(f"找不到 {args.seed}，改用純自動別名", file=sys.stderr)
        seed = {}

    records = {}
    for label, prof, daily, insecure in (
        # 櫃買伺服器憑證鏈不完整，允許降級重試，詳見 netutil.py
        ("TWSE", TWSE_PROFILE, TWSE_DAILY, False),
        ("TPEx", TPEX_PROFILE, TPEX_DAILY, args.allow_insecure_tpex),
    ):
        try:
            got = load_market(prof, daily, label, insecure)
            records.update(got)
            print(f"{label} 取得 {len(got)} 檔")
        except Exception as e:
            print(f"{label} 抓取失敗，本次略過：{type(e).__name__}: {e}",
                  file=sys.stderr)

    if not records:
        print("兩個市場都抓不到資料，中止。", file=sys.stderr)
        sys.exit(1)

    scored = []
    skipped = []
    for rec in records.values():
        mcap = compute_market_cap(rec)
        if mcap is None:
            skipped.append(rec["stock_id"])
            continue
        rec["market_cap"] = mcap
        scored.append(rec)

    scored.sort(key=lambda r: r["market_cap"], reverse=True)
    for i, rec in enumerate(scored, 1):
        rec["cap_rank"] = i

    top = scored[: args.top]
    today = datetime.now(TPE).strftime("%Y-%m-%d")

    with open(f"{args.outdir}/universe.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["date", "cap_rank", "stock_id", "market", "name_short",
                    "industry", "close", "market_cap_bn"])
        for r in top:
            w.writerow([today, r["cap_rank"], r["stock_id"], r["market"],
                        r["name_short"], r["industry"], r["close"],
                        round(r["market_cap"] / 1e9, 2)])

    # 全市場官方簡稱 -> 股號，用來擋掉去雜訊詞造成的撞名
    reserved_shorts = {}
    for rec in records.values():
        ns = (rec.get("name_short") or "").strip()
        if ns:
            reserved_shorts.setdefault(ns, rec["stock_id"])

    alias_map = {r["stock_id"]: generate_aliases(r, seed, reserved_shorts) for r in top}
    with open(f"{args.outdir}/aliases.json", "w", encoding="utf-8") as f:
        json.dump(alias_map, f, ensure_ascii=False, indent=2)

    exact, substr = find_collisions(alias_map)
    with open(f"{args.outdir}/collisions.txt", "w", encoding="utf-8-sig") as f:
        f.write(f"# 別名衝突報告 {today}\n\n")
        f.write("## 完全重複，必須人工刪除其一\n")
        for a, sids in exact:
            f.write(f"  {a} -> {', '.join(sids)}\n")
        f.write(f"\n## 子字串重疊，比對時務必長字串優先（共 {len(substr)} 組）\n")
        for s, so, l, lo in substr:
            f.write(f"  {s} ({','.join(so)})  包含於  {l} ({','.join(lo)})\n")

    print(f"universe.csv    {len(top)} 檔")
    print(f"aliases.json    {sum(len(v) for v in alias_map.values())} 條別名")
    print(f"collisions.txt  完全重複 {len(exact)} 組，子字串重疊 {len(substr)} 組")
    if skipped:
        print(f"缺價或缺資本額而略過 {len(skipped)} 檔：{','.join(skipped[:10])} ...")


if __name__ == "__main__":
    main()
