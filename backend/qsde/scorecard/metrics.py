"""
Pure, honest measurement math for the Scorecard (ARCHITECTURE.md §2).

DB-free, deterministic, fully testable. These are the numbers that tell you the
TRUTH about each engine — hit-rate, EXPECTANCY (the real goal, not accuracy),
and calibration — all on net-of-cost returns.
"""
from __future__ import annotations

from typing import Sequence

import numpy as np


def hit_rate(returns: Sequence[float]) -> float:
    r = np.asarray(list(returns), dtype=float)
    return float((r > 0).mean()) if r.size else 0.0


def expectancy(returns: Sequence[float]) -> float:
    """Mean per-trade net return. Positive = an edge. THIS, not accuracy, is the goal."""
    r = np.asarray(list(returns), dtype=float)
    return float(r.mean()) if r.size else 0.0


def win_loss_payoff(returns: Sequence[float]) -> dict:
    r = np.asarray(list(returns), dtype=float)
    wins, losses = r[r > 0], r[r < 0]
    avg_win = float(wins.mean()) if wins.size else 0.0
    avg_loss = float(losses.mean()) if losses.size else 0.0      # negative
    payoff = float(avg_win / abs(avg_loss)) if avg_loss != 0 else float("inf")
    return {"avg_win": avg_win, "avg_loss": avg_loss, "payoff_ratio": payoff,
            "n_win": int(wins.size), "n_loss": int(losses.size)}


def brier_score(confidences: Sequence[float], outcomes: Sequence[int]) -> float:
    """Calibration error: mean((confidence - outcome)^2). 0 = perfect, 0.25 = coin-flip."""
    c = np.asarray(list(confidences), dtype=float)
    o = np.asarray(list(outcomes), dtype=float)
    if c.size == 0 or c.size != o.size:
        return float("nan")
    return float(np.mean((c - o) ** 2))


def calibration_table(confidences: Sequence[float], outcomes: Sequence[int],
                      bins: int = 10) -> list[dict]:
    """Per-bin stated-confidence vs realized hit-rate. Calibrated => realized ~ mean_conf."""
    c = np.asarray(list(confidences), dtype=float)
    o = np.asarray(list(outcomes), dtype=float)
    edges = np.linspace(0.0, 1.0, bins + 1)
    out = []
    for i in range(bins):
        lo, hi = edges[i], edges[i + 1]
        mask = (c >= lo) & (c <= hi) if i == bins - 1 else (c >= lo) & (c < hi)
        n = int(mask.sum())
        out.append({
            "bin_lo": round(float(lo), 3), "bin_hi": round(float(hi), 3), "n": n,
            "mean_conf": round(float(c[mask].mean()), 4) if n else None,
            "realized": round(float(o[mask].mean()), 4) if n else None,
        })
    return out


def summarize(returns: Sequence[float], confidences: Sequence[float] | None = None,
              outcomes: Sequence[int] | None = None) -> dict:
    r = list(returns)
    s = {"n": len(r), "hit_rate": round(hit_rate(r), 4),
         "expectancy": round(expectancy(r), 6), **win_loss_payoff(r)}
    if confidences is not None and outcomes is not None:
        s["brier"] = round(brier_score(confidences, outcomes), 4)
    return s


__all__ = ["hit_rate", "expectancy", "win_loss_payoff", "brier_score",
           "calibration_table", "summarize"]
