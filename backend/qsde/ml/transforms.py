"""Per-date cross-sectional feature transforms for models that are NOT scale/outlier-invariant (linear, deep).

Tree models split on thresholds, so heavy tails do not hurt them and they keep the RAW values (audit trail). A linear or
neural model would be dominated by a handful of observations when a feature reaches hundreds of standard deviations
(`smc_*_dist` are absolute price distances: up to ~770 sd in the full-universe snapshot). For those models the heavy-tailed
columns are replaced by their per-date cross-sectional rank-gauss: rank within the date, mapped through the normal quantile
function. Using only same-date information cannot leak the future. NaN stays NaN (callers impute with a missing indicator).
"""
from __future__ import annotations

from typing import Sequence

import numpy as np
import pandas as pd
from scipy.special import ndtri

HEAVY_TAIL_SD = 20.0              # a column is 'heavy-tailed' if max |x - median| / std exceeds this


def heavy_tailed_columns(frame: pd.DataFrame, cols: Sequence[str], sd_limit: float = HEAVY_TAIL_SD) -> list:
    out = []
    for c in cols:
        x = frame[c].to_numpy(float)
        x = x[np.isfinite(x)]
        if len(x) < 100:
            continue
        sd = x.std()
        if sd > 0 and np.max(np.abs(x - np.median(x))) / sd > sd_limit:
            out.append(c)
    return out


def per_date_rank_gauss(frame: pd.DataFrame, cols: Sequence[str], date_col: str = "as_of_date") -> pd.DataFrame:
    """Return a copy with `cols` replaced by their same-date rank-gauss values (clipped to +-4 sd)."""
    out = frame.copy()
    g = out.groupby(date_col, sort=False)
    for c in cols:
        r = g[c].rank(method="average")
        n = g[c].transform("count")
        u = (r - 0.5) / n                                    # in (0, 1); NaN where the value was NaN
        out[c] = np.clip(ndtri(u.to_numpy(float)), -4.0, 4.0)
    return out


def model_matrix(frame: pd.DataFrame, feature_cols: Sequence[str], kind: str = "tree",
                 date_col: str = "as_of_date") -> tuple[pd.DataFrame, dict]:
    """Features as the given model family should see them.

    kind 'tree'   : raw values, untouched.
    kind 'linear' / 'deep' : heavy-tailed columns per-date rank-gauss transformed; the rest unchanged.
    Returns (frame_with_features, report). The raw column set is never modified in place.
    """
    if kind == "tree":
        return frame, {"kind": kind, "transformed": []}
    if kind not in ("linear", "deep"):
        raise ValueError("kind must be 'tree', 'linear' or 'deep'")
    heavy = heavy_tailed_columns(frame, feature_cols)
    return per_date_rank_gauss(frame, heavy, date_col), {"kind": kind, "transformed": heavy, "method": "per_date_rank_gauss"}
