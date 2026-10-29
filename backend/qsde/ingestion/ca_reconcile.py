"""ca_reconcile_v1: classify every NSE-published corporate action as confirmed / needs_review / rejected.

EVIDENCE ONLY. Nothing here adjusts a price; a separate, reviewed adjustment pipeline will consume the `confirmed` rows. The
checks per category:

  SPLIT_BONUS / CONSOLIDATION  rename-aware gap test of `corp_actions_v3` (hard bounds + 3-session persistence)
  DIVIDEND                     the symbol traded around the ex-date, the amount parses, the yield is sane (<= 25% of the last close)
  RIGHTS                       ratio and premium parse and the symbol traded (the terms are identified; no factor is invented)
  DEMERGER / OTHER             always needs_review; the biggest overnight gap near the ex-date is recorded as evidence
"""
from __future__ import annotations

import re
from typing import Optional

import numpy as np
import pandas as pd

from qsde.ingestion.corp_actions_v3 import evaluate as v3_evaluate
from qsde.ingestion.security_master import SecurityMap

VERSION = "ca_reconcile_v1"
MAX_DIV_YIELD = 0.25
_AMOUNT = re.compile(r"(?:rs\.?|re\.?|inr)\s*([0-9]+(?:\.[0-9]+)?)", re.I)
_RIGHTS = re.compile(r"rights?\s+(?:issue\s+)?(\d+)\s*:\s*(\d+)", re.I)
_PREMIUM = re.compile(r"premium\s*(?:of\s*)?(?:rs\.?|re\.?)?\s*([0-9]+(?:\.[0-9]+)?)", re.I)

COLS = ["action_id", "category", "symbol", "symbol_of_day", "security_id", "ex_date", "classification", "reason", "listed_factor",
        "observed_ratio", "factor_error", "gap_date", "shift_sessions", "untraded_sessions", "amount", "yield_pct", "terms"]


def parse_dividend(subject: str) -> tuple[Optional[float], int]:
    """Total cash amount per share in the text and how many amounts were found (several = interim + special, etc.)."""
    amounts = [float(x) for x in _AMOUNT.findall(subject or "")]
    return (sum(amounts) if amounts else None), len(amounts)


def parse_rights(subject: str) -> tuple[Optional[str], Optional[float]]:
    m, p = _RIGHTS.search(subject or ""), _PREMIUM.search(subject or "")
    return (f"{m.group(1)}:{m.group(2)}" if m else None), (float(p.group(1)) if p else None)


class _Bars:
    def __init__(self, prim: pd.DataFrame):
        px = prim[["symbol", "date", "open", "close"]].copy()
        px["date"] = pd.to_datetime(px["date"])
        self.by = {s: (g["date"].to_numpy("datetime64[ns]"), g["open"].to_numpy(float), g["close"].to_numpy(float))
                   for s, g in px.sort_values(["symbol", "date"]).groupby("symbol", sort=False)}
        self.last = px["date"].max()

    def around(self, sym: str, ex: pd.Timestamp):
        """(last close strictly before ex, index of first bar on/after ex, arrays) or None."""
        t = self.by.get(sym)
        if t is None:
            return None
        d, o, c = t
        i = int(np.searchsorted(d, np.datetime64(ex)))
        return (float(c[i - 1]) if i > 0 else None), i, t


def reconcile_actions(ca: pd.DataFrame, prim: pd.DataFrame, smap: Optional[SecurityMap], calendar) -> pd.DataFrame:
    """ca: rows of nse_corporate_actions (id, symbol, category, subject, ex_date, ...). prim: raw primary series (symbol, date, open, close)."""
    bars = _Bars(prim)
    out = []

    def base(r, sym_day):
        return dict.fromkeys(COLS) | {"action_id": int(r.id), "category": r.category, "symbol": r.symbol, "symbol_of_day": sym_day,
                                      "security_id": (smap.security_of(r.symbol) if smap is not None else None),
                                      "ex_date": pd.Timestamp(r.ex_date).date()}

    # ── split / bonus / consolidation: the v3 gap test
    sb = ca[ca["category"].isin(["SPLIT_BONUS", "CONSOLIDATION"])]
    if len(sb):
        unc = pd.DataFrame({"listed_symbol": sb["symbol"], "listed_ex_date": pd.to_datetime(sb["ex_date"]), "purpose": sb["subject"]})
        v3 = v3_evaluate(unc, prim, smap, calendar=calendar)
        for r, v in zip(sb.itertuples(), v3.itertuples()):
            b = base(r, v.symbol_of_day)
            b.update(listed_factor=v.listed_factor, observed_ratio=v.observed_ratio, factor_error=v.factor_error,
                     gap_date=v.gap_date, shift_sessions=v.shift_sessions, untraded_sessions=v.untraded_sessions, terms=r.subject[:120])
            if v.classification == "v3_candidate":
                b.update(classification="confirmed", reason=v.reason)
            elif v.classification == "v3_rejected":
                flat = v.observed_ratio is not None and v.observed_ratio == v.observed_ratio and 0.96 <= v.observed_ratio <= 1.04
                b.update(classification="rejected" if flat else "needs_review",
                         reason=("no price gap near the listed date: " if flat else "a gap exists but fails the bounds: ") + v.reason)
            else:
                nobars = v.reason.startswith("no price bars") or "no bars within" in v.reason
                b.update(classification="rejected" if nobars else "needs_review", reason=v.reason)
            out.append(b)

    # ── everything else
    rest = ca[~ca["category"].isin(["SPLIT_BONUS", "CONSOLIDATION"])]
    for r in rest.itertuples():
        ex = pd.Timestamp(r.ex_date)
        sym = smap.symbol_on(r.symbol, ex) if smap is not None else r.symbol
        b = base(r, sym)
        ctx = bars.around(sym, ex)
        if ctx is None:
            b.update(classification="rejected", reason=f"no price bars for {sym} (outside the equity price archive)")
            out.append(b)
            continue
        last_close, i, (d, o, c) = ctx
        if ex > bars.last:
            b.update(classification="needs_review", reason="pending: ex-date is after the latest price bar")
            out.append(b)
            continue
        if last_close is None or i >= len(d):
            b.update(classification="needs_review", reason="no bar before or after the ex-date to measure against")
            out.append(b)
            continue
        if r.category == "DIVIDEND":
            amt, n = parse_dividend(r.subject)
            b["amount"] = amt
            if amt is None:
                b.update(classification="needs_review", reason="dividend amount not parseable")
            else:
                y = amt / last_close
                b["yield_pct"] = y * 100
                if y > MAX_DIV_YIELD:
                    b.update(classification="needs_review", reason=f"dividend is {y:.0%} of the last close: check units / special payout")
                else:
                    b.update(classification="confirmed",
                             reason=f"Rs {amt:g}/share ({y:.2%} of last close)" + (f"; {n} amounts summed" if n > 1 else ""))
        elif r.category == "RIGHTS":
            ratio, prem = parse_rights(r.subject)
            b.update(terms=f"{ratio} @ premium {prem}" if ratio else None, amount=prem)
            if ratio and prem is not None:
                b.update(classification="confirmed", reason="rights terms identified (ratio and premium); no price factor derived")
            else:
                b.update(classification="needs_review", reason="rights terms incomplete in the NSE text")
        else:                                           # DEMERGER, OTHER
            j = [i - 1, i, i + 1]
            gaps = [o[k] / c[k - 1] - 1.0 for k in j if 0 < k < len(d)]
            worst = max(gaps, key=abs) if gaps else None
            b.update(observed_ratio=(1 + worst) if worst is not None else None, classification="needs_review",
                     reason=f"{r.category.lower()}: reviewed individually; largest overnight gap near the ex-date "
                            f"{'n/a' if worst is None else format(worst, '+.1%')}")
        out.append(b)
    return pd.DataFrame(out, columns=COLS)
