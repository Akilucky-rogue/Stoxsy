"""Portfolio cockpit maths (pure functions): valuation, allocation, concentration, P&L, pro-forma history, drawdown, benchmark comparison.

Honesty rules baked in:
  * P&L is unrealised, from the file's average cost and the latest NSE close (prices are the adjusted series, whose latest value equals the traded price).
  * The history is PRO-FORMA: today's quantities held constant over the window. It is not the portfolio's actual past value (we do not know past trades).
  * The benchmark is a tradable proxy (e.g. NIFTYBEES, a Nifty 50 ETF, price return), labelled as such, not the index itself.
"""
from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

STALE_DAYS = 5
POSITION_LIMIT = 0.08                    # same single-name cap as the decision cards


def value_holdings(h: pd.DataFrame, last_px: pd.DataFrame, as_of: pd.Timestamp) -> pd.DataFrame:
    """h: symbol, quantity, avg_cost. last_px: symbol, date, close (latest per symbol). Returns one row per holding with value, cost, P&L, weight, stale flag."""
    df = h.merge(last_px.rename(columns={"date": "price_date", "close": "last_price"}), on="symbol", how="left")
    df["value"] = df["quantity"] * df["last_price"]
    df["cost"] = df["quantity"] * df["avg_cost"]
    df["pnl"] = df["value"] - df["cost"]
    df["pnl_pct"] = np.where(df["cost"] > 0, df["pnl"] / df["cost"].replace(0, np.nan), np.nan)
    total = df["value"].sum(skipna=True)
    df["weight"] = df["value"] / total if total > 0 else np.nan
    df["stale"] = (pd.Timestamp(as_of) - pd.to_datetime(df["price_date"])).dt.days > STALE_DAYS
    return df.sort_values("value", ascending=False).reset_index(drop=True)


def concentration(weights: pd.Series, limit: float = POSITION_LIMIT) -> dict:
    w = weights.dropna().sort_values(ascending=False)
    w = w / w.sum() if w.sum() > 0 else w
    hhi = float((w ** 2).sum()) if len(w) else float("nan")
    return {"n_positions": int(len(w)), "top1": float(w.iloc[0]) if len(w) else None, "top3": float(w.head(3).sum()) if len(w) else None,
            "top5": float(w.head(5).sum()) if len(w) else None, "top10": float(w.head(10).sum()) if len(w) else None,
            "hhi": hhi, "effective_n": float(1.0 / hhi) if hhi and hhi > 0 else None,
            "above_limit": [{"symbol": s, "weight": float(v)} for s, v in w[w > limit].items()], "limit": limit}


def history(h: pd.DataFrame, closes: pd.DataFrame, bench: Optional[pd.Series], lookback_days: int = 365) -> Optional[dict]:
    """Pro-forma value of the current quantities over the window, its drawdown, and the same-window benchmark. closes: wide (date x symbol) adjusted closes."""
    q = h.set_index("symbol")["quantity"]
    cols = [c for c in q.index if c in closes.columns]
    if not cols:
        return None
    px = closes[cols].sort_index()
    end = px.index.max()
    px = px[px.index >= end - pd.Timedelta(days=lookback_days)]
    px = px.ffill().dropna(how="all")
    full = px.dropna()                                  # only dates where every held name has a price (names listed mid-window shorten the window)
    if len(full) < 30:
        return None
    val = (full * q[cols]).sum(axis=1)
    ret = val.pct_change().dropna()
    eq = val / val.iloc[0]
    dd = eq / eq.cummax() - 1.0
    out = {"start": str(full.index[0].date()), "end": str(full.index[-1].date()), "sessions": int(len(full)),
           "names_priced": len(cols), "names_missing": [s for s in q.index if s not in cols],
           "total_return": float(eq.iloc[-1] - 1.0), "ann_volatility": float(ret.std() * np.sqrt(252)), "max_drawdown": float(dd.min()),
           "current_drawdown": float(dd.iloc[-1]), "var95_1d": float(-np.percentile(ret, 5)), "worst_day": float(ret.min()),
           "series": {"date": [str(d.date()) for d in eq.index], "value": [float(x) for x in eq.values], "drawdown": [float(x) for x in dd.values]}}
    if bench is not None:
        from qsde.funds.returns import trim_after_unit_change
        bench, _ = trim_after_unit_change(bench.astype(float))        # a benchmark ETF with an unadjusted unit change must not show a false cliff
        b = bench.sort_index().reindex(full.index).ffill().dropna()
        if len(b) >= 30:
            bret = b.pct_change().dropna()
            j = pd.concat([ret, bret], axis=1, join="inner").dropna()
            beq = b / b.iloc[0]
            bdd = beq / beq.cummax() - 1.0
            beta = float(np.cov(j.iloc[:, 0], j.iloc[:, 1])[0, 1] / np.var(j.iloc[:, 1], ddof=1)) if len(j) > 30 and np.var(j.iloc[:, 1], ddof=1) > 0 else None
            out["benchmark"] = {"total_return": float(beq.iloc[-1] - 1.0), "ann_volatility": float(bret.std() * np.sqrt(252)), "max_drawdown": float(bdd.min()),
                                "beta": beta, "correlation": float(j.corr().iloc[0, 1]) if len(j) > 30 else None,
                                "series": {"date": [str(d.date()) for d in beq.index], "value": [float(x) for x in beq.values]}}
    return out


def defensive_profile(df: pd.DataFrame, scores: Optional[pd.DataFrame]) -> dict:
    """Share of portfolio value by defensive bucket (1 = highest risk .. 5 = most defensive); names outside the signal universe are 'not scored'."""
    if scores is None or scores.empty:
        return {"available": False, "buckets": {}, "scored_share": 0.0}
    m = df.merge(scores[["symbol", "risk_bucket", "defensive_score"]], on="symbol", how="left")
    tot = m["value"].sum(skipna=True)
    out: dict = {}
    for b, g in m.groupby(m["risk_bucket"].fillna(0).astype(int)):
        out["not scored" if b == 0 else str(b)] = {"share": float(g["value"].sum() / tot) if tot > 0 else 0.0, "names": g["symbol"].tolist()}
    scored = float(m.loc[m["risk_bucket"].notna(), "value"].sum() / tot) if tot > 0 else 0.0
    return {"available": True, "buckets": out, "scored_share": scored}


def summarise(df: pd.DataFrame) -> dict:
    priced = df[df["value"].notna()]
    value, cost = float(priced["value"].sum()), float(priced["cost"].sum())
    return {"value": value, "cost": cost, "pnl": value - cost, "pnl_pct": (value - cost) / cost if cost > 0 else None,
            "n_holdings": int(len(df)), "n_unpriced": int(df["value"].isna().sum()), "n_stale": int(df["stale"].sum()),
            "winners": int((priced["pnl"] > 0).sum()), "losers": int((priced["pnl"] < 0).sum())}
