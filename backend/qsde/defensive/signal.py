"""Defensive / low-risk allocation signal: rank names by downside risk, not by expected return.

This is portfolio construction, NOT a directional prediction. The full-universe research evaluation found that the model's
score was 0.91 rank-correlated with low volatility; this module makes that exposure explicit, auditable and point-in-time.

Everything at date t uses data up to and including the close of t (rolling windows only; `test_defensive.py` proves that appending
future bars cannot change a past score). Higher `defensive_score` = lower risk.

Components (percent-ranked within the eligible set on each date, equal weight):
  atr_pct      14-session average true range / close          (lower is better)
  vol60        60-session std of daily returns                 (lower)
  downside60   60-session downside deviation (returns < 0)    (lower)
  dd120        drawdown from the trailing 120-session high     (shallower)
  liq          trailing 60-session median traded value         (higher)
"""
from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

COMPONENTS = ("atr_pct", "vol60", "downside60", "dd120", "liq")
LOWER_IS_BETTER = {"atr_pct": True, "vol60": True, "downside60": True, "dd120": False, "liq": False}
MIN_HISTORY = 120


def risk_features(prices: pd.DataFrame) -> pd.DataFrame:
    """prices: symbol, date, open, high, low, close, volume (adjusted). Returns one row per symbol-date with the component values."""
    out = []
    for sym, g in prices.sort_values(["symbol", "date"]).groupby("symbol", sort=False):
        c, h, l, v = g["close"].astype(float), g["high"].astype(float), g["low"].astype(float), g["volume"].astype(float)
        pc = c.shift(1)
        tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
        r = c.pct_change()
        neg = r.where(r < 0, 0.0)
        f = pd.DataFrame({
            "symbol": sym, "date": g["date"].to_numpy(),
            "atr_pct": (tr.rolling(14, min_periods=14).mean() / c).to_numpy(),
            "vol60": r.rolling(60, min_periods=40).std().to_numpy(),
            "downside60": np.sqrt((neg ** 2).rolling(60, min_periods=40).mean()).to_numpy(),
            "dd120": (c / c.rolling(120, min_periods=60).max() - 1.0).to_numpy(),
            "liq": (c * v).rolling(60, min_periods=40).median().to_numpy(),
            "n_hist": np.arange(1, len(g) + 1)})
        out.append(f)
    return pd.concat(out, ignore_index=True)


def defensive_scores(feat: pd.DataFrame, eligible: Optional[pd.DataFrame] = None, with_warnings: bool = True) -> pd.DataFrame:
    """Adds defensive_score in [0, 1] (1 = most defensive), risk_bucket 1..5 (5 = most defensive) and a plain-language warning list.

    `eligible`: optional frame (date, symbol) restricting the cross-section each date (the point-in-time universe)."""
    f = feat.copy()
    if eligible is not None:
        f = f.merge(eligible[["date", "symbol"]].drop_duplicates(), on=["date", "symbol"], how="inner")
    f = f[f["n_hist"] >= MIN_HISTORY].dropna(subset=list(COMPONENTS)).copy()
    g = f.groupby("date")
    parts = []
    for c in COMPONENTS:
        rk = g[c].rank(pct=True)
        parts.append(1.0 - rk if LOWER_IS_BETTER[c] else rk)
    f["defensive_score"] = sum(parts) / len(parts)
    f["risk_bucket"] = f.groupby("date")["defensive_score"].transform(
        lambda s: pd.qcut(s.rank(method="first"), 5, labels=False) + 1 if len(s) >= 25 else np.nan)
    if not with_warnings:
        f["warnings"] = ""
        return f
    warn = []
    for r in f[["atr_pct", "vol60", "dd120", "liq", "defensive_score"]].itertuples(index=False):
        w = []
        if r.defensive_score < 0.2:
            w.append("high-risk quintile")
        if r.dd120 < -0.35:
            w.append(f"{r.dd120:.0%} below its 120-session high")
        if r.atr_pct > 0.06:
            w.append(f"daily range ~{r.atr_pct:.1%}")
        warn.append("; ".join(w))
    f["warnings"] = warn
    return f


def regime_state(universe_ret: pd.Series) -> pd.DataFrame:
    """Universe-level state from an equal-weight return series (known at each date, expanding percentiles only):
    risk_off = 63-session trend negative AND 21-session vol above its expanding 66th percentile; risk_on = trend positive AND vol below the median."""
    r = universe_ret.sort_index()
    trend = (1 + r).rolling(63).apply(np.prod, raw=True) - 1.0
    vol = r.rolling(21).std()
    pct = vol.expanding(min_periods=252).apply(lambda x: (x[:-1] < x[-1]).mean() if len(x) > 1 else np.nan, raw=True)
    state = pd.Series("neutral", index=r.index)
    state[(trend < 0) & (pct > 0.66)] = "risk_off"
    state[(trend > 0) & (pct < 0.5)] = "risk_on"
    state[trend.isna() | pct.isna()] = "unknown"
    return pd.DataFrame({"trend63": trend, "vol21": vol, "vol_pct": pct, "regime": state})


# ── portfolio construction ───────────────────────────────────────────

def weights(scores_on_date: pd.DataFrame, scheme: str, regime: str = "neutral") -> pd.Series:
    """Long-only weights summing to 1 for one date. Schemes: ew | tilt | tilt_gated | top_defensive."""
    s = scores_on_date.set_index("symbol")["defensive_score"]
    n = len(s)
    if n == 0:
        return pd.Series(dtype=float)
    if scheme == "ew":
        w = pd.Series(1.0, index=s.index)
    elif scheme in ("tilt", "tilt_gated"):
        w = 0.5 + s                                              # 0.5x (least defensive) .. 1.5x (most defensive) of equal weight
        if scheme == "tilt_gated" and regime == "risk_off":
            w = w.where(s >= s.quantile(0.2), w * 0.5)           # halve the highest-risk quintile when the universe is risk-off
    elif scheme == "top_defensive":
        w = (s >= s.quantile(0.8)).astype(float)
    else:
        raise ValueError(f"unknown scheme {scheme!r}")
    return w / w.sum()


def backtest(prices: pd.DataFrame, scores: pd.DataFrame, regimes: pd.DataFrame, scheme: str, rebalance_every: int = 21,
             one_way_cost_bps: float = 12.5, lag: int = 1, winsor: float = 0.4) -> tuple[pd.Series, pd.Series]:
    """Daily net returns and daily turnover. Weights are set from the close of day t and executed `lag` sessions later (default: the
    next close), so a signal never earns the move that produced it. Costs are charged on traded notional at each rebalance."""
    px = prices.pivot(index="date", columns="symbol", values="close").sort_index()
    ret = px.pct_change().clip(-winsor, winsor).fillna(0.0)
    dates = ret.index
    sdates = sorted(scores["date"].unique())
    by_date = {d: g for d, g in scores.groupby("date")}
    start = max(0, int(np.searchsorted(dates, sdates[0])))
    rb = list(range(start, len(dates), rebalance_every))
    cur = pd.Series(dtype=float)
    daily, turn = [], []
    pos_of_rb = {p: i for i, p in enumerate(rb)}
    for t in range(start, len(dates) - lag - 1):
        d = dates[t]
        traded = 0.0
        if t in pos_of_rb and d in by_date:
            reg = regimes["regime"].get(d, "neutral") if regimes is not None else "neutral"
            new = weights(by_date[d], scheme, reg)
            both = cur.index.union(new.index)
            traded = float((new.reindex(both).fillna(0) - cur.reindex(both).fillna(0)).abs().sum())
            cur = new
            exec_t = t + lag
        # returns of the day after the (lagged) execution date
        if len(cur):
            r = ret.iloc[t + lag + 1].reindex(cur.index).fillna(0.0)
            gross = float((cur * r).sum())
            cur = (cur * (1.0 + r))
            cur = cur / cur.sum()
        else:
            gross = 0.0
        daily.append((dates[t + lag + 1], gross - traded * one_way_cost_bps / 1e4))
        turn.append((dates[t + lag + 1], traded))
    s = pd.Series(dict(daily)).sort_index()
    return s, pd.Series(dict(turn)).sort_index()


def stats(r: pd.Series, ann: int = 252) -> dict:
    r = r.dropna()
    if len(r) < 2 or r.std() == 0:
        return {}
    eq = (1 + r).cumprod()
    dd = eq / eq.cummax() - 1.0
    return {"ann_return": float(eq.iloc[-1] ** (ann / len(r)) - 1), "ann_vol": float(r.std() * np.sqrt(ann)),
            "sharpe": float(r.mean() / r.std() * np.sqrt(ann)), "max_dd": float(dd.min()),
            "calmar": float((eq.iloc[-1] ** (ann / len(r)) - 1) / abs(dd.min())) if dd.min() < 0 else np.nan,
            "downside_dev": float(np.sqrt((r.clip(upper=0) ** 2).mean() * ann)), "worst_month": float(r.rolling(21).sum().min()), "n_days": int(len(r))}
