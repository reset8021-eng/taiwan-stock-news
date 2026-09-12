"""
link.py — 把新聞標題對應到股號

用法：
    from link import Linker
    linker = Linker("aliases.json")
    linker.link("台積電法說會釋出資本支出上修訊號")   # -> ['2330']

比對順序（先命中先算，命中後把該段字挖空避免重複命中）：
    1. 括號內的四位股號
    2. 長度優先的別名比對
       中文別名用單純子字串比對
       英文別名要求單字邊界，避免 ASE 誤中 ELASER
"""

import json
import re

GROUP_TERMS = {
    "台塑四寶": ["1301", "1303", "1326", "6505"],
    "鴻海集團": ["2317"],
    "長榮集團": ["2603", "2618"],
    "遠東集團": ["1402", "4904"],
    "台泥集團": ["1101"],
}

# 出現這些詞代表講的是族群不是個股，不指定 primary
SECTOR_ONLY = re.compile(r"概念股|族群|類股|老三雄")

UNVERIFIED = re.compile(r"^\s*(傳|傳出|市場傳|外傳|盛傳)|傳將|傳斥資")

LATIN = re.compile(r"^[A-Za-z0-9 .&\-]+$")


class Linker:
    def __init__(self, alias_path="aliases.json"):
        with open(alias_path, encoding="utf-8") as f:
            self.aliases = json.load(f)
        self.table = sorted(
            ((a, sid) for sid, al in self.aliases.items() for a in al),
            key=lambda x: -len(x[0]),
        )
        self.patterns = []
        for alias, sid in self.table:
            if LATIN.match(alias):
                pat = re.compile(
                    r"(?<![A-Za-z0-9])" + re.escape(alias) + r"(?![A-Za-z0-9])",
                    re.IGNORECASE,
                )
            else:
                pat = re.compile(re.escape(alias))
            self.patterns.append((pat, alias, sid))

    def link(self, title):
        """回傳 [(stock_id, relevance), ...]，第一筆為 primary。"""
        hits, seen = [], set()
        buf = title

        for m in re.finditer(r"[(（](\d{4})[)）]", title):
            code = m.group(1)
            if code in self.aliases and code not in seen:
                hits.append((m.start(), code))
                seen.add(code)

        for term, sids in GROUP_TERMS.items():
            idx = buf.find(term)
            if idx >= 0:
                for off, sid in enumerate(sids):
                    if sid not in seen:
                        hits.append((idx + off * 0.001, sid))
                        seen.add(sid)
                buf = buf.replace(term, "\x00" * len(term))

        for pat, alias, sid in self.patterns:
            if sid in seen:
                continue
            m = pat.search(buf)
            if m:
                hits.append((m.start(), sid))
                seen.add(sid)
                buf = buf[: m.start()] + "\x00" * (m.end() - m.start()) + buf[m.end():]

        if not hits:
            return []

        # primary 取標題中最先出現的那檔，不是別名最長的那檔
        ordered = [sid for _, sid in sorted(hits, key=lambda x: x[0])]
        sector = bool(SECTOR_ONLY.search(title))
        out = [(ordered[0], "direct" if sector else "primary")]
        out += [(s, "direct") for s in ordered[1:]]
        return out

    def is_unverified(self, title):
        return bool(UNVERIFIED.search(title))


if __name__ == "__main__":
    lk = Linker()
    tests = [
        "台積電(2330)董事會通過先進封裝擴產案",
        "長榮航空第三季營收創同期新高",
        "長榮海運運價連四週下滑",
        "南亞科報價回升，南亞塑膠同步受惠",
        "傳鴻海獲北美客戶追加AI機櫃訂單",
        "統一超商調整鮮食供應鏈",
        "AI散熱概念股全面走揚",
    ]
    for t in tests:
        print(f"{t}\n  -> {lk.link(t)}  unverified={lk.is_unverified(t)}\n")
