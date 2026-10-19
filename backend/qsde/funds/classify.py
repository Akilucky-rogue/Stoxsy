"""Mutual-fund / ETF classification from AMFI's own labels (section heading + scheme name). Rule-based; every row keeps its evidence string.

AMFI headings come in several eras: `Open Ended Schemes(Debt Scheme - Banking and PSU Fund)` (current SEBI categories),
`Open Ended Schemes ( Income/Debt Oriented Schemes - Liquid Fund )` and `Open Ended Schemes ( Money Market )` (older), `Index Funds - Equity Funds`,
`Fund of Funds Scheme (Domestic)` ... All are handled; anything that cannot be placed stays `unclassified` WITH a reason (it is never forced into a category).

Output
  asset_class  equity | debt | hybrid | commodity | international | solution | other
  fund_group   etf | index_fund | liquid_or_overnight | arbitrage | fund_of_funds | solution_oriented | closed_end | interval | fixed_term | standard | unclassified
  flags        is_etf, is_index, is_liquid, is_fof, is_direct, is_growth
"""
from __future__ import annotations

import re
from typing import Optional

HEADING = re.compile(r"^\s*(Open Ended|Close Ended|Closed Ended|Interval Fund|Interval)\s*(?:Schemes?)?\s*\(\s*(.*?)\s*\)\s*$", re.I)
OVERSEAS = re.compile(r"overseas|nasdaq|s&p 500|\bnyse\b|\bus\b|\busa\b|global|international|world|china|hang seng|europe|japan|fang|msci (?:world|eafe|em)|\bemerging\b|\bbrazil\b|\bvietnam\b", re.I)
EQ_NAME = re.compile(r"nifty|sensex|equity|bluechip|large cap|midcap|mid cap|small cap|smallcap|\bbank\b|\bit\b|pharma|momentum|quality|value|low vol|alpha|dividend|\bcpse\b|\bpsu\b|infra|consumption|next 50|top 100|total market", re.I)
DEBT_NAME = re.compile(r"\bdebt\b|liquid|overnight|\bg-?sec\b|gilt|\bsdl\b|bond|\bpsu\b.*debt|money market|income|floating|treasury|corporate|banking and psu|constant maturity|target maturity|\bt-?bill", re.I)


# A closed-ended scheme is a fixed-term plan only if its NAME says so; "series" or "plan" alone also appear in ordinary closed-end equity schemes.
FIXED_TERM = re.compile(r"fixed (term|maturity|horizon|tenure)|\bfmp\b|capital protection|\b\d{2,4}\s*(days|day|months|month)\b", re.I)


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


def _asset_from_name(n: str) -> Optional[str]:
    if re.search(r"\bgold\b|\bsilver\b", n):
        return "commodity"
    if OVERSEAS.search(n):
        return "international"
    if DEBT_NAME.search(n) and not EQ_NAME.search(n.replace("psu", "")):
        return "debt"
    if re.search(r"hybrid|balanced|asset allocat|multi asset|arbitrage|equity savings", n):
        return "hybrid"
    if EQ_NAME.search(n):
        return "equity"
    return None


def classify(heading: Optional[str], name: Optional[str], plan: Optional[str] = None, option: Optional[str] = None) -> dict:
    typ, broad, sub = split_heading(heading)
    b, s, n = (broad or "").lower(), (sub or "").lower(), (name or "").lower()
    ev: list = []
    asset = "other"
    # ── asset class from the heading ──
    if "solution" in b or "retirement" in b or "children" in b:
        asset = "solution"; ev.append("heading: solution oriented")
    elif "hybrid" in b or "balanced" in b or "arbitrage" in s or "arbitrage" in b:
        asset = "hybrid"; ev.append("heading: hybrid / arbitrage")
    elif "equity" in b and not b.startswith("index") or b in ("growth", "elss") or "elss" in s or "equity linked" in b:
        asset = "equity"; ev.append("heading: equity")
    elif "debt" in b or "income" in b or b in ("gilt", "money market", "liquid", "floating rate", "liquid fund") or "debt" in s:
        asset = "debt"; ev.append("heading: debt / income")
    elif b.startswith("index funds"):
        asset = "debt" if "debt" in s else "equity" if "equity" in s else "other"; ev.append(f"heading: index funds - {s or 'unspecified'}")
    else:
        ev.append("heading: other / unlabelled")
    is_etf = bool(re.search(r"\betf\b|exchange traded|\bbees\b", n) or "etf" in s)
    is_fof = bool("fof" in s or "fund of fund" in b or "fund of fund" in s or "fund of fund" in n or re.search(r"\bfof\b", n) or "fof" in b)
    is_index = bool("index fund" in s or b.startswith("index funds") or (re.search(r"\bindex\b", n) and not is_etf))
    is_liquid = bool("liquid" in s or "overnight" in s or b in ("liquid", "liquid fund") or re.search(r"\bliquid\b|\bovernight\b", n))
    # ── commodity / overseas overrides and underlying for index funds and funds of funds ──
    if re.search(r"\bgold\b|\bsilver\b", n) or "gold" in s or "silver" in s:
        asset = "commodity"; ev.append("name/heading: gold or silver")
    elif (is_fof or is_index or is_etf) and (OVERSEAS.search(n) or "overseas" in s or "overseas" in b):
        asset = "international"; ev.append("fund/index/ETF tracking overseas markets")
    elif asset == "other" and (is_fof or is_index or is_etf):
        a = _asset_from_name(n)
        if a:
            asset = a; ev.append(f"underlying inferred from the scheme name: {a}")
    # ── fund group (what kind of vehicle) ──
    reason = None
    if is_etf:
        group = "etf"
    elif is_liquid and asset in ("debt", "other"):
        group = "liquid_or_overnight"
    elif "arbitrage" in s or "arbitrage" in n:
        group = "arbitrage"
    elif is_fof:
        group = "fund_of_funds"
    elif is_index:
        group = "index_fund"
    elif asset == "solution":
        group = "solution_oriented"
    elif typ and typ.startswith("Clos"):
        group = "fixed_term" if FIXED_TERM.search(n) else "closed_end"
    elif typ and typ.startswith("Interval"):
        group = "interval"
    elif asset != "other":
        group = "standard"
    else:
        group, reason = "unclassified", "heading does not map to an asset class and the name gives no unambiguous underlying"
    if group == "arbitrage" and asset in ("debt", "equity"):
        asset = "hybrid"; ev.append("arbitrage funds are hybrid (equity-taxed) by SEBI category, regardless of the older heading")
    if asset == "other" and group != "unclassified":
        reason = "vehicle known (" + group + ") but the underlying asset class could not be determined from the heading or the name"
        ev.append("asset class undetermined; vehicle known")
    pl, op = (plan or "").lower(), (option or "").lower()
    is_direct = True if "direct" in pl or "direct" in n else (False if ("regular" in pl or "regular" in n) else None)
    is_growth = True if ("growth" in op or "growth" in n or "accumulation" in op) else (False if re.search(r"idcw|dividend|payout|bonus", op + " " + n) else None)
    return {"scheme_type": typ, "broad_category": broad, "sub_category": sub, "asset_class": asset, "fund_group": group, "unclassified_reason": reason,
            "is_etf": is_etf, "is_index": is_index, "is_liquid": is_liquid, "is_fof": is_fof, "is_direct": is_direct, "is_growth": is_growth,
            "classification_evidence": "; ".join(ev) or "no rule matched"}
