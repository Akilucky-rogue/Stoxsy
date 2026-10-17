"""
Chart/indicator fidelity vs the market (ARCHITECTURE.md §2).

"Are our numbers what actually printed?" Reconcile our computed OHLC / indicator
series against the broker's official values. This is a CORRECTNESS check and
must score ~100% — the one place high accuracy is real and demanded (it answers
the user's "compare our charts to live markets").
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def reconcile_series(ours: pd.Series, official: pd.Series, *, rel_tol: float = 1e-3) -> dict:
    """Align two indexed series on their shared index; report match stats.

    match_pct = fraction of shared points within rel_tol (relative). For a
    correct computation this should be ~1.0.
    """
    o, k = ours.dropna(), official.dropna()
    shared = o.index.intersection(k.index)
    n = len(shared)
    if n == 0:
        return {"n": 0, "match_pct": None, "max_abs_err": None, "max_rel_err": None}
    a = o.loc[shared].astype(float).to_numpy()
    b = k.loc[shared].astype(float).to_numpy()
    abs_err = np.abs(a - b)
    denom = np.where(np.abs(b) > 1e-9, np.abs(b), 1.0)
    rel_err = abs_err / denom
    return {
        "n": n,
        "match_pct": round(float((rel_err <= rel_tol).mean()), 4),
        "max_abs_err": round(float(abs_err.max()), 6),
        "max_rel_err": round(float(rel_err.max()), 6),
    }


def reconcile_ohlc(ours: pd.DataFrame, official: pd.DataFrame, *, rel_tol: float = 1e-3) -> dict:
    """Reconcile OHLC of our bars vs official bars (whichever columns are shared)."""
    cols = [c for c in ("open", "high", "low", "close")
            if c in ours.columns and c in official.columns]
    if not cols:
        return {"error": "no shared OHLC columns", "overall_match_pct": None}
    per_col = {c: reconcile_series(ours[c], official[c], rel_tol=rel_tol) for c in cols}
    pcts = [v["match_pct"] for v in per_col.values() if v["match_pct"] is not None]
    return {"per_column": per_col,
            "overall_match_pct": round(float(np.mean(pcts)), 4) if pcts else None}


__all__ = ["reconcile_series", "reconcile_ohlc"]
