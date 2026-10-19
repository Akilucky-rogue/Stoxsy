"""NAV-based returns, labelled honestly.

For each horizon the return is NAV(asof) / NAV(on or before asof - horizon) - 1 (cumulative). For horizons of a year or more an annualised figure
(CAGR) is added; it is NEVER mixed into the cumulative column. A horizon is reported only if the scheme has a NAV on or before the start date
(no extrapolation), the start NAV is not stale (> 7 days older than the target date), and no single-day NAV move above 50% falls inside the window.

Basis: for a GROWTH option NAV change is a total return after expenses. For a payout / IDCW option the NAV falls when income is paid, so
the figure is a PRICE return only and is labelled so.
"""
from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

HORIZONS = {"1D": ("days", 1), "1W": ("days", 7), "1M": ("months", 1), "3M": ("months", 3), "6M": ("months", 6),
            "1Y": ("months", 12), "3Y": ("months", 36), "5Y": ("months", 60)}
STALE_DAYS = 7
MAX_DAILY_MOVE = 0.5          # a single-day NAV move above 50% inside a window disqualifies that horizon (unit consolidation, face-value change, bad print)


def _start(asof: pd.Timestamp, kind: str, n: int) -> pd.Timestamp:
    return asof - pd.Timedelta(days=n) if kind == "days" else asof - pd.DateOffset(months=n)


def scheme_returns(nav: pd.Series, asof: Optional[pd.Timestamp] = None) -> dict:
    """nav: float Series indexed by date (ascending). Returns {horizon: {"cumulative": x, "annualised": y|None, "start": date}}."""
    nav = nav.dropna().sort_index()
    if nav.empty:
        return {}
    asof = pd.Timestamp(asof) if asof is not None else nav.index[-1]
    nav = nav[nav.index <= asof]
    if nav.empty:
        return {}
    end_d, end_v = nav.index[-1], float(nav.iloc[-1])
    out = {}
    for h, (kind, n) in HORIZONS.items():
        target = _start(end_d, kind, n)
        prior = nav[nav.index <= target]
        if prior.empty or (target - prior.index[-1]).days > STALE_DAYS:
            continue
        v0, d0 = float(prior.iloc[-1]), prior.index[-1]
        if v0 <= 0:
            continue
        seg = nav[(nav.index >= d0)].pct_change().dropna()
        if len(seg) and seg.abs().max() > MAX_DAILY_MOVE:             # a unit consolidation / face-value change / bad print inside the window: the NAV ratio is not a return
            continue
        cum = end_v / v0 - 1.0
        years = (end_d - d0).days / 365.25
        out[h] = {"cumulative": cum, "annualised": (1.0 + cum) ** (1.0 / years) - 1.0 if n >= 12 and kind == "months" and years > 0.5 else None,
                  "start": str(d0.date()), "end": str(end_d.date())}
    return out


def risk_metrics(nav: pd.Series, window_days: int = 756) -> dict:
    """Volatility, max drawdown and downside deviation from daily NAV changes over the last `window_days` (~3y). Annualised with 252."""
    nav = nav.dropna().sort_index().tail(window_days + 1)
    r = nav.pct_change().dropna()
    r = r[np.isfinite(r)]
    if len(r) < 60:
        return {}
    eq = nav / nav.iloc[0]
    dd = (eq / eq.cummax() - 1.0).min()
    return {"ann_volatility": float(r.std() * np.sqrt(252)), "max_drawdown": float(dd),
            "downside_deviation": float(np.sqrt((r.clip(upper=0) ** 2).mean() * 252)), "observations": int(len(r))}
