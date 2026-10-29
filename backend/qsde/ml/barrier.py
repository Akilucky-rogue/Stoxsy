"""One barrier walker, shared by labels, the paper journal and the Scorecard resolver.

A label that is built differently from the outcome paper trading resolves is a
different strategy: the model would be graded on one thing and judged on another.
This module is the only place that decides what a stop, a target and a gap through
either mean.

Rules (long; short mirrors):
  * Walk the bars AFTER entry in order; the entry bar itself is not in the window.
  * Gap-through: if a bar OPENS at/through the stop (or target) the exit fills at the
    OPEN, not at the barrier price. A stop filled at its trigger price after an
    overnight gap is a fiction; a gap through a target is a (real) favourable fill.
  * Intrabar: low <= stop -> LOSS at the stop; high >= target -> WIN at the target.
  * Same-bar tie (both reachable): STOP wins (pessimistic).
  * No barrier hit across the window: TIME exit at the CLOSE of the last window bar.
  * Without an `open` column the gap rule is skipped (legacy behaviour).
"""
from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd


def walk_barriers(bars: pd.DataFrame, stop: Optional[float], target: Optional[float],
                  direction: int, sessions: Optional[int] = None) -> Optional[dict]:
    """bars: ascending, columns high/low/close (+ optional open, date).

    Returns {status, exit_price, exit_idx (0-based within the window), exit_date, gap_exit}
    as soon as a barrier is hit, or the TIME exit when the full window has no hit;
    None if the window is incomplete and nothing was hit yet.
    """
    if bars is None or len(bars) == 0:
        return None
    window = bars.head(int(sessions)) if sessions else bars
    has_open = "open" in window.columns
    dates = window["date"].tolist() if "date" in window.columns else list(range(len(window)))
    hi = window["high"].to_numpy(float)
    lo = window["low"].to_numpy(float)
    cl = window["close"].to_numpy(float)
    op = window["open"].to_numpy(float) if has_open else None
    d = 1 if direction > 0 else -1

    for i in range(len(window)):
        if d > 0:
            if has_open and stop is not None and op[i] <= stop:
                return _out("LOSS", op[i], i, dates, True)
            if has_open and target is not None and op[i] >= target:
                return _out("WIN", op[i], i, dates, True)
            if stop is not None and lo[i] <= stop:                       # stop first on a tie
                return _out("LOSS", stop, i, dates, False)
            if target is not None and hi[i] >= target:
                return _out("WIN", target, i, dates, False)
        else:
            if has_open and stop is not None and op[i] >= stop:
                return _out("LOSS", op[i], i, dates, True)
            if has_open and target is not None and op[i] <= target:
                return _out("WIN", op[i], i, dates, True)
            if stop is not None and hi[i] >= stop:
                return _out("LOSS", stop, i, dates, False)
            if target is not None and lo[i] <= target:
                return _out("WIN", target, i, dates, False)

    if sessions and len(window) < int(sessions):
        return None                                                       # window not complete yet
    return _out("TIME", cl[-1], len(window) - 1, dates, False)


def _out(status: str, px: float, idx: int, dates: list, gap: bool) -> dict:
    return {"status": status, "exit_price": float(px), "exit_idx": int(idx),
            "exit_date": dates[idx], "gap_exit": bool(gap)}


def walk_barriers_matrix(O: np.ndarray, H: np.ndarray, L: np.ndarray, C: np.ndarray,
                         stop: np.ndarray, target: np.ndarray, side: np.ndarray):
    """Vectorised twin of `walk_barriers` for many events at once (used by label building).

    O/H/L/C: (n_events, h) window matrices (NaN where the window is incomplete).
    stop/target: (n_events,) price levels. side: (n_events,) +1/-1.
    Returns (status [0 time, +1 win, -1 loss], exit_idx, exit_price, gap_exit).
    Must agree with `walk_barriers` bar-for-bar; tests enforce it.
    """
    n, h = H.shape
    long_ = side[:, None] > 0
    s = stop[:, None]
    t = target[:, None]

    with np.errstate(invalid="ignore"):                # NaN windows compare False
        gap_stop = np.where(long_, O <= s, O >= s)
        gap_tgt = np.where(long_, O >= t, O <= t)
        in_stop = np.where(long_, L <= s, H >= s)
        in_tgt = np.where(long_, H >= t, L <= t)

    # Same priority order as walk_barriers, per bar: gap-stop, gap-target, intrabar stop, intrabar target.
    code = np.select([gap_stop, gap_tgt, in_stop, in_tgt], [1, 2, 3, 4], default=0)
    any_hit = code > 0
    first = np.where(any_hit.any(axis=1), any_hit.argmax(axis=1), -1)

    status = np.zeros(n, dtype=int)
    exit_idx = np.where(first >= 0, first, h - 1)
    rows = np.arange(n)
    gap = np.zeros(n, dtype=bool)
    exit_px = C[rows, exit_idx].copy()
    hit = first >= 0
    if hit.any():
        r = rows[hit]
        k = first[hit]
        c = code[r, k]
        status[r] = np.where((c == 1) | (c == 3), -1, 1)
        gap[r] = (c == 1) | (c == 2)
        exit_px[r] = np.select([c == 1, c == 2, c == 3], [O[r, k], O[r, k], stop[r]], default=target[r])
    return status, exit_idx, exit_px, gap


# ── fill convention ──────────────────────────────────────────────────

def levels_at_fill(ref_price: Optional[float], fill_price: float, stop: Optional[float],
                   target: Optional[float]) -> tuple[Optional[float], Optional[float]]:
    """Re-anchor stop/target from the signal-time reference (close of t) to the actual next-open fill.

    The decision is taken at the close of t and filled at the open of t+1, so the executable barriers are
    `fill * (1 -/+ offset)` with the SAME fractional offsets the signal carried - exactly how the label builder
    constructs them (stop = entry * (1 - sl_mult * sigma)). Ratio-scaling preserves the offsets for longs and
    shorts alike. Without a usable reference the absolute levels are returned unchanged (explicit fills).
    """
    if ref_price is None or not np.isfinite(ref_price) or ref_price <= 0 or fill_price is None or fill_price <= 0:
        return stop, target
    k = float(fill_price) / float(ref_price)
    return (None if stop is None else float(stop) * k), (None if target is None else float(target) * k)
