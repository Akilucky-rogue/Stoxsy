"""
Pairs / statistical-arbitrage math (the StatArb family — ARCHITECTURE.md §engines).

PURE + DB-free. Engle-Granger cointegration + Ornstein-Uhlenbeck-style mean
reversion on the residual spread:

    spread_t = A_t - beta * B_t - alpha          (OLS hedge of A on B)
    z_t      = (spread_t - mean_W) / std_W       (rolling window W)

A pair is TRADEABLE only when ALL hold (anti-data-mining gates):
  * Engle-Granger p-value <= max_pvalue          (spread is actually stationary)
  * min_half_life <= half-life <= max_half_life  (reverts, but not instantly/never)
  * positive hedge ratio                         (a clean long-A/short-B relationship)

The half-life comes from the AR(1) fit  Δspread_t = lambda * spread_{t-1} + c,
hl = -ln 2 / lambda  (only finite when lambda < 0, i.e. mean-reverting).

Nothing here trades or touches the DB — `evaluate_pair` returns a verdict dict the
engine turns into a defined-risk PAIR idea, and the resolver replays the same spread
math forward to score it. Keeping it pure is what makes both sides testable + honest.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def hedge_ratio(a, b) -> tuple[float, float]:
    """OLS hedge of A on B: returns (beta, alpha) for A ~ alpha + beta*B."""
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)
    beta, alpha = np.polyfit(b, a, 1)          # slope, intercept
    return float(beta), float(alpha)


def spread(a, b, beta: float, alpha: float = 0.0) -> np.ndarray:
    """Residual spread A - beta*B - alpha (in A's price units)."""
    return np.asarray(a, dtype=float) - beta * np.asarray(b, dtype=float) - alpha


def half_life(spread_arr, *, min_obs: int = 20, sig_t: float = 2.0) -> float:
    """Mean-reversion half-life (sessions) from the AR(1) fit Δs = λ·s_{t-1} + c.

    Returns inf unless λ is SIGNIFICANTLY negative — a t-test on the AR coefficient
    (t = λ̂/se(λ̂), the standard OLS-slope test) must clear `sig_t`. A merely-negative
    λ from noise is not evidence of reversion; requiring significance is the same
    coefficient-significance discipline as a regression z-score. NaN if < min_obs."""
    s = np.asarray(spread_arr, dtype=float)
    s = s[~np.isnan(s)]
    if len(s) < min_obs:
        return float("nan")
    lag = s[:-1]
    delta = np.diff(s)
    n = len(delta)
    lam, c = np.polyfit(lag, delta, 1)         # delta ~ lam*lag + c
    if lam >= 0 or n <= 2:
        return float("inf")                    # not mean-reverting
    # OLS slope standard error: se = sqrt(RSS/(n-2)) / sqrt(Σ(x-x̄)²)
    resid = delta - (lam * lag + c)
    ss_x = float(np.sum((lag - lag.mean()) ** 2))
    if ss_x <= 0:
        return float("inf")
    se_lam = float(np.sqrt(np.sum(resid ** 2) / (n - 2)) / np.sqrt(ss_x))
    if se_lam <= 0 or (lam / se_lam) > -sig_t:  # not significantly < 0
        return float("inf")
    return float(-np.log(2) / lam)


def latest_zscore(spread_arr, lookback: int) -> tuple[float, float]:
    """(z, sigma) of the LAST spread value vs its trailing `lookback` window.
    NaN z / 0 sigma when the window is too short or degenerate (no signal)."""
    s = pd.Series(spread_arr, dtype=float).dropna()
    if len(s) < lookback:
        return float("nan"), float("nan")
    window = s.iloc[-lookback:]
    mu = float(window.mean())
    sigma = float(window.std(ddof=1))
    if not (sigma > 0):                          # flat/degenerate window -> no z
        return float("nan"), 0.0
    return float((s.iloc[-1] - mu) / sigma), sigma


def engle_granger_pvalue(a, b) -> float:
    """Engle-Granger cointegration p-value (low => spread is stationary).

    The EG two-step is ORDER-DEPENDENT: coint(a,b) regresses a on b, coint(b,a)
    regresses b on a — different residuals, different p-values. We take the
    CONSERVATIVE max of both directions, so a pair only clears the gate if it is
    cointegrated regardless of which leg is the regressor. This removes the
    arbitrary leg-order dependence and tightens the false-positive rate (the gate
    is the load-bearing anti-data-mining defense). NaN on failure; statsmodels
    imported lazily so the module loads without it."""
    try:
        from statsmodels.tsa.stattools import coint
        a = np.asarray(a, float)
        b = np.asarray(b, float)
        p_ab = float(coint(a, b)[1])
        p_ba = float(coint(b, a)[1])
        return max(p_ab, p_ba)
    except Exception:  # noqa: BLE001
        return float("nan")


def evaluate_pair(a, b, *, lookback: int = 250, min_half_life: float = 1.0,
                  max_half_life: float = 30.0, max_pvalue: float = 0.05,
                  require_positive_beta: bool = True) -> dict:
    """One pair -> a verdict dict. Aligns the series, then fits EVERYTHING on the
    SAME trailing `lookback` window — hedge, spread, mean/sigma, half-life, and the
    EG test — so the residual is genuinely zero-mean on that window and all three
    gates describe the same equilibrium (no full-history-hedge vs recent-window-z
    inconsistency). `tradeable` is the gate the engine acts on."""
    df = pd.concat([pd.Series(a, dtype=float).rename("a"),
                    pd.Series(b, dtype=float).rename("b")], axis=1).dropna()
    if len(df) < lookback:
        return {"ok": False, "reason": "insufficient_data", "n": int(len(df))}
    df = df.iloc[-lookback:]                    # ONE window for all gates

    beta, alpha = hedge_ratio(df["a"], df["b"])
    if require_positive_beta and not (beta > 0):
        return {"ok": False, "reason": "non_positive_beta", "beta": beta, "n": int(len(df))}

    sp = spread(df["a"], df["b"], beta, alpha)
    z, sigma = latest_zscore(sp, lookback)      # window == full sp now
    spread_mean = float(pd.Series(sp).mean())   # ~0: OLS residual is zero-mean here
    spread_now = float(pd.Series(sp).iloc[-1])
    hl = half_life(sp)
    pval = engle_granger_pvalue(df["a"], df["b"])

    tradeable = bool(
        pval == pval and pval <= max_pvalue
        and min_half_life <= hl <= max_half_life
        and sigma > 0 and z == z
    )
    return {
        "ok": True, "tradeable": tradeable,
        "beta": float(beta), "alpha": float(alpha), "spread_sigma": float(sigma),
        "spread_mean": spread_mean, "spread_now": spread_now,
        "z": float(z), "half_life": float(hl), "pvalue": float(pval), "n": int(len(df)),
        "price_a": float(df["a"].iloc[-1]), "price_b": float(df["b"].iloc[-1]),
    }


__all__ = ["hedge_ratio", "spread", "half_life", "latest_zscore",
           "engle_granger_pvalue", "evaluate_pair"]
