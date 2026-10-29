"""Per-scheme snapshot: labelled returns, risk, data-quality badge and peer context. Pure functions (the script does the database I/O).

Labels (never mixed): total_return_after_expenses (growth option), price_return_only (IDCW / payout line), price_return (ETF), option_unknown.
A peer group is only formed from comparable lines (same asset class and category, same plan, growth lines, or ETFs of the same asset class), needs at least
MIN_PEERS members with a valid figure, and excludes schemes whose data-quality badge is `poor`. Peer context is descriptive, not a recommendation.
"""
from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

from qsde.funds.returns import MAX_DAILY_MOVE, risk_metrics_detailed, scheme_returns_detailed

MIN_PEERS = 8
PEER_HORIZONS = ("1Y", "3Y", "5Y")
GAP_DAYS = 30
RECENT_YEARS = 5

BASIS_TEXT = {
    "total_return_after_expenses": "Total return after expenses (growth option)",
    "price_return_only": "Price return only: payouts are not in the NAV file (IDCW / payout line)",
    "price_return": "ETF: price return (no growth option; any distributions are not included)",
    "option_unknown": "Option not identified: treat as a price return only",
}


def basis_of(is_growth: Optional[bool], is_etf: bool) -> str:
    if is_growth is True:
        return "total_return_after_expenses"
    if is_growth is False:
        return "price_return_only"
    return "price_return" if is_etf else "option_unknown"


def peer_key(meta: dict, basis: str) -> Optional[str]:
    if basis == "price_return":
        return f"etf|{meta.get('asset_class')}"
    if basis != "total_return_after_expenses":
        return None                                                     # IDCW and unidentified lines are not comparable with growth lines
    plan = "direct" if meta.get("is_direct") is True else "regular" if meta.get("is_direct") is False else "na"
    return f"{meta.get('asset_class')}|{meta.get('sub_category') or meta.get('broad_category') or meta.get('fund_group')}|{plan}"


def quality(nav: pd.Series, meta: dict, basis: str) -> dict:
    """{"level": ok|caution|poor, "reasons": [...], "flags": {...}} from the NAV series and the classification."""
    nav = nav.dropna().sort_index()
    reasons_poor, reasons_caution = [], []
    flags: dict = {}
    end = nav.index[-1]
    r = nav.pct_change().dropna()
    recent = r[r.index >= end - pd.DateOffset(years=RECENT_YEARS)]
    jumps = recent[recent.abs() > MAX_DAILY_MOVE]
    flags["unit_change_moves_5y"] = int(len(jumps))
    if len(jumps):
        reasons_poor.append(f"{len(jumps)} one-day NAV move(s) above 50% in the last {RECENT_YEARS} years (latest {jumps.index[-1].date()}): returns across them are not shown")
    gap = nav.index.to_series().diff().dt.days
    gap3 = gap[gap.index >= end - pd.DateOffset(years=3)]
    flags["gaps_over_30d_3y"] = int((gap3 > GAP_DAYS).sum())
    if flags["gaps_over_30d_3y"]:
        reasons_caution.append(f"{flags['gaps_over_30d_3y']} gap(s) of more than {GAP_DAYS} days between NAVs in the last 3 years (longest {int(gap3.max())} days)")
    hist_days = int((end - nav.index[0]).days)
    flags["history_days"] = hist_days
    if hist_days < 365:
        reasons_caution.append(f"only {hist_days} days of NAV history: no 1-year figure")
    if meta.get("fund_group") == "unclassified":
        reasons_poor.append("classification unresolved: " + str(meta.get("unclassified_reason") or "no rule matched"))
    elif meta.get("asset_class") == "other":
        reasons_caution.append("asset class could not be determined from the AMFI heading or name")
    if basis == "option_unknown":
        reasons_caution.append("growth / IDCW option not identified")
    flags["classification_uncertain"] = bool(meta.get("fund_group") == "unclassified" or meta.get("asset_class") == "other" or basis == "option_unknown")
    level = "poor" if reasons_poor else "caution" if reasons_caution else "ok"
    return {"level": level, "reasons": reasons_poor + reasons_caution, "flags": flags}


def _ann(r: dict, h: str) -> Optional[float]:
    x = r.get(h)
    return x.get("annualised") if x and x.get("status") == "ok" else None


def scheme_snapshot(nav: pd.Series, meta: dict) -> Optional[dict]:
    nav = nav.dropna().sort_index()
    if len(nav) < 2:
        return None
    basis = basis_of(meta.get("is_growth"), bool(meta.get("is_etf")))
    rets = scheme_returns_detailed(nav)
    risk = risk_metrics_detailed(nav)
    q = quality(nav, meta, basis)
    return {"scheme_code": int(meta["scheme_code"]), "as_of": nav.index[-1].date(), "basis": basis, "returns": rets, "risk": risk, "quality": q, "peers": None,
            "ret_1y": _ann(rets, "1Y"), "ret_3y": _ann(rets, "3Y"), "ret_5y": _ann(rets, "5Y"),
            "vol_3y": risk.get("ann_volatility") if risk.get("status") == "ok" else None, "maxdd_3y": risk.get("max_drawdown") if risk.get("status") == "ok" else None,
            "quality_level": q["level"], "_peer_key": peer_key(meta, basis)}


def add_peer_context(rows: list) -> None:
    """Fills row["peers"] in place: peer group size, the peer median and this scheme's percentile (share of peers it beat) per horizon."""
    groups: dict = {}
    for r in rows:
        if r["_peer_key"] and r["quality_level"] != "poor":
            groups.setdefault(r["_peer_key"], []).append(r)
    for key, members in groups.items():
        out_h = {}
        for h in PEER_HORIZONS:
            vals = [(m, _ann(m["returns"], h)) for m in members]
            vals = [(m, v) for m, v in vals if v is not None]
            if len(vals) < MIN_PEERS:
                continue
            arr = np.array([v for _, v in vals])
            for m, v in vals:
                m.setdefault("_ph", {})[h] = {"percentile": float((arr < v).sum() / (len(arr) - 1)) if len(arr) > 1 else None, "median": float(np.median(arr)), "n": int(len(arr))}
            out_h[h] = len(arr)
        for m in members:
            if m.get("_ph"):
                m["peers"] = {"key": key, "group_size": len(members), "horizons": m["_ph"]}
