"""Mutual-fund / ETF classification from AMFI's own labels (section heading + scheme name). Rule-based; every row keeps its evidence string.

AMFI headings look like `Open Ended Schemes(Debt Scheme - Banking and PSU Fund)` (current SEBI categories) or `Open Ended Schemes ( Money Market )`
(older labels). Output: asset_class (equity | debt | hybrid | commodity | international | solution | other) plus orthogonal flags
is_etf, is_index, is_liquid, is_fof, is_direct, is_growth.
"""
from __future__ import annotations

import re
from typing import Optional

HEADING = re.compile(r"^\s*(Open Ended|Close Ended|Interval Fund|Interval)\s*(?:Schemes?)?\s*\(\s*(.*?)\s*\)\s*$", re.I)


def split_heading(h: Optional[str]) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """'Open Ended Schemes(Debt Scheme - Banking and PSU Fund)' -> ('Open Ended', 'Debt Scheme', 'Banking and PSU Fund')."""
    if not isinstance(h, str):
        return None, None, None
    m = HEADING.match(h.replace("’", "'"))
    if not m:
        return None, None, h.strip()
    typ, inner = m.group(1).title(), m.group(2)
    if " - " in inner:
        broad, sub = inner.split(" - ", 1)
        return typ, broad.strip(), sub.strip()
    return typ, inner.strip(), None


def classify(heading: Optional[str], name: Optional[str], plan: Optional[str] = None, option: Optional[str] = None) -> dict:
    typ, broad, sub = split_heading(heading)
    b, s = (broad or "").lower(), (sub or "").lower()
    n = (name or "").lower()
    ev = []
    asset = "other"
    if b.startswith("equity") or b in ("growth", "elss", "equity linked savings scheme") or "elss" in s:
        asset = "equity"; ev.append("heading: equity")
    elif b.startswith("debt") or b in ("income", "gilt", "money market", "liquid", "debt oriented"):
        asset = "debt"; ev.append("heading: debt")
    elif b.startswith("hybrid") or b in ("balanced", "balanced scheme"):
        asset = "hybrid"; ev.append("heading: hybrid")
    elif b.startswith("solution"):
        asset = "solution"; ev.append("heading: solution oriented")
    elif b.startswith("other") or b == "":
        ev.append("heading: other / unlabelled")
    is_etf = bool(re.search(r"\betf\b|exchange traded", n) or "etf" in s)
    is_index = bool("index fund" in s or re.search(r"\bindex\b", n) and not is_etf)
    is_fof = bool("fof" in s or "fund of fund" in n or "fund of funds" in n or "fof" in n)
    is_liquid = bool("liquid" in s or "overnight" in s or re.search(r"\bliquid\b|\bovernight\b", n))
    if re.search(r"\bgold\b|\bsilver\b", n) or "gold" in s or "silver" in s:
        asset = "commodity"; ev.append("name/heading: gold or silver")
    if is_fof and ("overseas" in s or re.search(r"overseas|nasdaq|s&p 500|\bus\b|global|international|world|china|hang seng|europe|japan", n)):
        asset = "international"; ev.append("fund of funds investing overseas")
    elif re.search(r"nasdaq|hang seng|s&p 500|\bnyse\b|fang|msci (world|eafe|em)", n) and (is_etf or is_index or is_fof):
        asset = "international"; ev.append("name: overseas index")
    if is_etf and asset == "other":
        ev.append("etf with no asset heading")
    pl, op = (plan or "").lower(), (option or "").lower()
    is_direct = True if "direct" in pl or "direct" in n else (False if ("regular" in pl or "regular" in n) else None)
    is_growth = True if ("growth" in op or "growth" in n or "accumulation" in op) else (False if re.search(r"idcw|dividend|payout|bonus", op + " " + n) else None)
    return {"scheme_type": typ, "broad_category": broad, "sub_category": sub, "asset_class": asset, "is_etf": is_etf, "is_index": is_index,
            "is_liquid": is_liquid, "is_fof": is_fof, "is_direct": is_direct, "is_growth": is_growth, "classification_evidence": "; ".join(ev) or "no rule matched"}
