"""
Implied-volatility analytics for the F&O-income engine (F&O increment 3).

Turns a parsed option frame (qsde.fno.bhavcopy) into the chain snapshot the
FnoIncomeEngine consumes:
    {underlying, spot, t_years, atm_iv, iv_rank, expiry, chain:{(strike,'CE'|'PE'):premium}}

All pure (BS inversion + simple stats). `iv_rank` takes an injected ATM-IV
history (persisted daily in production) so it stays unit-testable.
"""
from __future__ import annotations

from datetime import date

import pandas as pd

from qsde.fno.black_scholes import DEFAULT_R, implied_vol


def add_iv(options: pd.DataFrame, spot: float, asof: date, r: float = DEFAULT_R) -> pd.DataFrame:
    """Add `t_years` and `iv` columns (BS-inverted from settle). NaN where T<=0
    or the settle is below intrinsic / unquotable."""
    df = options.copy()
    days = (pd.to_datetime(df["expiry"]) - pd.Timestamp(asof)).dt.days.clip(lower=0)
    df["t_years"] = days / 365.0
    df["iv"] = [
        implied_vol(float(s), float(spot), float(k), float(t), str(ot), r=r) if t > 0 else float("nan")
        for s, k, t, ot in zip(df["settle"], df["strike"], df["t_years"], df["opt_type"])
    ]
    return df


def atm_iv(iv_df: pd.DataFrame, spot: float) -> float:
    """ATM implied vol = mean(CE-IV, PE-IV) at the strike nearest spot."""
    if iv_df.empty:
        return float("nan")
    atm_strike = iv_df.iloc[(iv_df["strike"] - spot).abs().argsort().iloc[0]]["strike"]
    ivs = iv_df.loc[iv_df["strike"] == atm_strike, "iv"].dropna()
    return float(ivs.mean()) if len(ivs) else float("nan")


def iv_rank(current_iv: float, history) -> float:
    """Where current ATM IV sits in its own [min,max] history (0..1).

    Returns NaN when the rank is NOT informative — no usable history/current, OR
    a degenerate range (hi <= lo: a single prior session or an all-flat window).
    NaN (not 0.5/"neutral") is deliberate: a non-informative rank must NOT be
    mistaken for "vol is at the median". The premium-selling gate treats NaN as
    "do not sell", so a cold/flat window suppresses emission instead of sneaking
    past a 0.5 threshold."""
    hist = [float(x) for x in history if x == x]            # drop NaN
    if not hist or current_iv != current_iv:
        return float("nan")
    lo, hi = min(hist), max(hist)
    if hi <= lo:
        return float("nan")
    return float(max(0.0, min(1.0, (current_iv - lo) / (hi - lo))))


def build_chain_snapshot(underlying: str, spot: float, asof: date, expiry: date,
                         iv_df: pd.DataFrame, iv_history) -> dict:
    """Assemble the FnoIncomeEngine source snapshot for one (underlying, expiry)."""
    sub = iv_df[iv_df["expiry"] == expiry]
    chain = {(float(r["strike"]), str(r["opt_type"])): float(r["settle"])
             for _, r in sub.iterrows()}
    a_iv = atm_iv(sub, spot)
    t_years = max((pd.Timestamp(expiry) - pd.Timestamp(asof)).days, 0) / 365.0
    return {
        "underlying": underlying,
        "spot": float(spot),
        "t_years": t_years,
        "atm_iv": a_iv,
        "iv_rank": iv_rank(a_iv, iv_history),
        "expiry": expiry,
        "chain": chain,
    }


__all__ = ["add_iv", "atm_iv", "iv_rank", "build_chain_snapshot"]
