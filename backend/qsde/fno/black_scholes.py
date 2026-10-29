"""
Black-Scholes pricing, implied volatility, and Greeks — the F&O-income engine's
math foundation (ARCHITECTURE.md §F&O).

Pure, deterministic, dependency-free (stdlib `math` only). Indian INDEX options
(NIFTY / BANKNIFTY) are European + cash-settled, so plain Black-Scholes is the
right model. Used to:
  * compute IV from NSE Bhavcopy settle prices (historical vol surface),
  * attach Greeks (delta/gamma/theta/vega) to every option TradeIdea,
  * drive the vol-regime filter (sell premium only when IV is rich).

Conventions: time `T` in YEARS, rate `r` continuous, prices/strikes in the same
unit. `vega` is per 1 vol-point (1%); `theta` is per calendar day.
"""
from __future__ import annotations

import math
from math import erf, exp, log, pi, sqrt

DEFAULT_R = 0.065   # ~India 1y risk-free; override per call if needed.


def _is_call(opt_type: str) -> bool:
    return str(opt_type).strip().upper().startswith("C")   # C / CE / CALL


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + erf(x / sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    return exp(-0.5 * x * x) / sqrt(2.0 * pi)


def _d1_d2(S: float, K: float, T: float, r: float, sigma: float) -> tuple[float, float]:
    d1 = (log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * sqrt(T))
    return d1, d1 - sigma * sqrt(T)


def bs_price(S: float, K: float, T: float, r: float, sigma: float, opt_type: str) -> float:
    """European Black-Scholes price. Returns intrinsic value when T<=0 or sigma<=0."""
    call = _is_call(opt_type)
    if T <= 0 or sigma <= 0:
        return max(S - K, 0.0) if call else max(K - S, 0.0)
    d1, d2 = _d1_d2(S, K, T, r, sigma)
    if call:
        return S * _norm_cdf(d1) - K * exp(-r * T) * _norm_cdf(d2)
    return K * exp(-r * T) * _norm_cdf(-d2) - S * _norm_cdf(-d1)


def bs_greeks(S: float, K: float, T: float, r: float, sigma: float, opt_type: str) -> dict:
    """delta, gamma, theta (per day), vega (per 1% vol). Long-option sign convention."""
    call = _is_call(opt_type)
    if T <= 0 or sigma <= 0:
        intrinsic_delta = (1.0 if S > K else 0.0) if call else (-1.0 if S < K else 0.0)
        return {"delta": intrinsic_delta, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
    d1, d2 = _d1_d2(S, K, T, r, sigma)
    pdf = _norm_pdf(d1)
    delta = _norm_cdf(d1) if call else _norm_cdf(d1) - 1.0
    gamma = pdf / (S * sigma * sqrt(T))
    vega = S * pdf * sqrt(T) / 100.0                       # per 1 vol-point
    decay = -(S * pdf * sigma) / (2.0 * sqrt(T))
    if call:
        theta = (decay - r * K * exp(-r * T) * _norm_cdf(d2)) / 365.0
    else:
        theta = (decay + r * K * exp(-r * T) * _norm_cdf(-d2)) / 365.0
    return {"delta": delta, "gamma": gamma, "theta": theta, "vega": vega}


def implied_vol(price: float, S: float, K: float, T: float, opt_type: str, *,
                r: float = DEFAULT_R, lo: float = 1e-4, hi: float = 5.0,
                tol: float = 1e-6, max_iter: int = 100) -> float:
    """Invert Black-Scholes for sigma via bisection (robust). Returns NaN if the
    price is below intrinsic / non-positive / outside the [lo,hi] vol bracket."""
    call = _is_call(opt_type)
    if T <= 0 or price <= 0:
        return float("nan")
    intrinsic = max(S - K, 0.0) if call else max(K - S, 0.0)
    if price < intrinsic - 1e-6:
        return float("nan")   # arbitrage / bad quote

    def f(sig: float) -> float:
        return bs_price(S, K, T, r, sig, opt_type) - price

    f_lo, f_hi = f(lo), f(hi)
    if f_lo * f_hi > 0:
        return float("nan")   # vol outside bracket
    for _ in range(max_iter):
        mid = 0.5 * (lo + hi)
        f_mid = f(mid)
        if abs(f_mid) < tol:
            return mid
        if f_lo * f_mid < 0:
            hi, f_hi = mid, f_mid
        else:
            lo, f_lo = mid, f_mid
    return 0.5 * (lo + hi)


__all__ = ["bs_price", "bs_greeks", "implied_vol", "DEFAULT_R"]
