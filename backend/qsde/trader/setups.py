"""Research setups for the Trader desk: simple, named, rule-based BASELINES. They generate candidates; they are not an engine and carry no claim of edge.

donchian20_breakout: today's close is above the highest high of the previous 20 sessions, on at least the median volume of those sessions, for a stock in the point-in-time top-500 universe
that is not in the two highest-risk defensive buckets. (A turtle-style breakout: the classic baseline that every learned model has to beat.) Candidates are ordered by the defensive score
(lower recent risk first), not by any belief that the breakout will pay: ranking by a forecast would claim an edge nobody has measured.
"""
from __future__ import annotations

from typing import Optional

import pandas as pd

SETUP = "donchian20_breakout"
LOOKBACK = 20


def breakouts(prices: pd.DataFrame) -> pd.DataFrame:
    """prices: symbol, date, high, close, volume (sorted or not). Returns one row per symbol that broke out on its LAST bar."""
    rows = []
    for sym, d in prices.sort_values("date").groupby("symbol"):
        if len(d) < LOOKBACK + 1:
            continue
        today, prev = d.iloc[-1], d.iloc[-(LOOKBACK + 1):-1]
        hi = float(prev["high"].max())
        if today["close"] > hi and today["volume"] >= prev["volume"].median():
            rows.append({"symbol": sym, "date": today["date"], "close": float(today["close"]), "prior_20d_high": hi, "breakout_pct": float(today["close"] / hi - 1.0),
                         "volume_x_median": float(today["volume"] / max(prev["volume"].median(), 1.0))})
    return pd.DataFrame(rows, columns=["symbol", "date", "close", "prior_20d_high", "breakout_pct", "volume_x_median"])


def candidates(read_sql, signal: Optional[pd.DataFrame], limit: int = 12, min_bucket: int = 3) -> pd.DataFrame:
    """Breakouts among the latest point-in-time universe, filtered by risk bucket and ordered by defensive score (highest = most defensive first)."""
    uni = read_sql("SELECT symbol FROM universe_pit WHERE method = 'adv60_top500_v1' AND date = (SELECT max(date) FROM universe_pit WHERE method = 'adv60_top500_v1')")
    px = read_sql("SELECT symbol, date, high, close, volume FROM ohlcv WHERE symbol = ANY(:s) AND date >= CURRENT_DATE - 60 ORDER BY symbol, date", params={"s": uni["symbol"].tolist()})
    px["date"] = pd.to_datetime(px["date"])
    b = breakouts(px)
    if b.empty:
        return b
    last = px["date"].max()
    b = b[b["date"] == last]                                  # a stale last bar is not a breakout today
    if signal is not None and len(signal):
        b = b.merge(signal[["symbol", "risk_bucket", "defensive_score"]], on="symbol", how="left")
        b = b[b["risk_bucket"].fillna(0) >= min_bucket].sort_values("defensive_score", ascending=False)
    else:
        b = b.sort_values("volume_x_median", ascending=False)
    return b.head(limit).reset_index(drop=True)
