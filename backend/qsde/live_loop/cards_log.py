"""Forward logging of the defensive signal / decision-card model into the immutable prediction ledger (shadow mode: logged, never traded).

Every name in the signal universe is logged each session, per horizon, with the signal score and rank, the size the decision cards would give it (0 when not selected)
and full lineage. The model makes NO return forecast, so `p_win`, `expected_return` and the interval are left empty: the loop measures whether the ordering helps
(rank IC, top-quintile net result) instead of grading a probability the model never claimed.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict

import numpy as np
import pandas as pd

MODEL_VERSION = "defensive_cards_v1"
HORIZON_IDS = ("swing_5d", "position_20d")


def realised_sigma(close: pd.Series, span: int) -> float:
    """EWM volatility of daily returns at the last close: the same construction `make_labels` uses for the barrier unit."""
    r = close.pct_change().dropna()
    return float(r.ewm(span=span, min_periods=min(60, span)).std().iloc[-1]) if len(r) >= 60 else float("nan")


def feature_hash(scores: pd.DataFrame) -> str:
    cols = ["symbol", "atr_pct", "vol60", "downside60", "dd120", "liq", "defensive_score", "risk_bucket"]
    d = scores[cols].sort_values("symbol").round(10)
    return hashlib.sha256(d.to_csv(index=False).encode()).hexdigest()[:32]


def signal_predictions(scores: pd.DataFrame, last_close: pd.Series, sigma: dict, weights: dict, prediction_time: pd.Timestamp, params: dict, lineage: dict,
                       model_version: str = MODEL_VERSION) -> pd.DataFrame:
    """scores: the defensive signal (one session). last_close: symbol -> close. sigma: {horizon_id: Series symbol -> sigma}. weights: {horizon_id: {symbol: weight}}.
    Returns one row per (symbol, horizon) in the ledger's input format."""
    as_of = pd.Timestamp(scores["date"].iloc[0]).normalize()
    rank = scores["defensive_score"].rank(pct=True)
    fh = feature_hash(scores)
    rows = []
    for hid in weights:
        w, sg = weights[hid], sigma.get(hid, pd.Series(dtype=float))
        for r, rk in zip(scores.itertuples(), rank):
            s = sg.get(r.symbol, np.nan)
            px = last_close.get(r.symbol, np.nan)
            rows.append({"model_version": model_version, "symbol": r.symbol, "as_of_date": as_of, "prediction_time": prediction_time, "horizon_id": hid, "side": 1,
                         "score": float(r.defensive_score), "rank_pct": float(rk), "ref_price": float(px) if np.isfinite(px) else None,
                         "sigma": float(s) if np.isfinite(s) else None, "position_size": float(w.get(r.symbol, 0.0)), "regime": str(r.regime),
                         "feature_hash": fh, "params": {**params, "horizon": hid, "note": "defensive risk ordering; no return forecast"}, **lineage})
    return pd.DataFrame(rows)


def portfolio_params_dict(p) -> dict:
    d = asdict(p)
    d.pop("holidays", None)
    return json.loads(json.dumps(d, default=str))
