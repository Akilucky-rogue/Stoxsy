"""Decision cards: WHAT to hold, WHERE it trades, HOW MUCH to hold, and TILL WHEN - as risk-based allocation, not a return forecast.

The research so far has not found a model with a net, out-of-sample edge (swing_5d REJECT; the predictive score was ~0.91 rank-correlated
with low volatility). So a card does not say "this will go up". It says: these are the lowest-risk liquid names today, here is the position
size that makes a stop-out cost at most a chosen share of capital, here is the stop, and here is when the position is reviewed or exits by time.

Sizing (all mechanical):
    stop distance   = sl_mult * sigma           (registry barrier multiplier for the horizon; sigma = EWM daily vol at the close)
    risk budget     = capital * risk_per_position * regime multiplier
    value           = min(risk budget / stop distance, capital * max_position, adv_participation * median daily traded value)
    quantity        = floor(value / last close)                       entry is the NEXT OPEN; stops that gap through fill at the open
Total exposure is capped by regime (risk-on 95%, neutral 80%, risk-off 50% of capital). Nothing here places an order.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

import numpy as np
import pandas as pd

from qsde.ml.registry import get_horizon

EVIDENCE = "RESEARCH_ONLY"
REGIME_RISK_MULT = {"risk_on": 1.0, "neutral": 0.8, "risk_off": 0.5, "unknown": 0.5}
REGIME_EXPOSURE_CAP = {"risk_on": 0.95, "neutral": 0.80, "risk_off": 0.50, "unknown": 0.50}


@dataclass(frozen=True)
class PortfolioParams:
    capital: float = 1_500_000.0
    risk_per_position: float = 0.005          # fraction of capital lost if a position is stopped out
    max_position: float = 0.08                # fraction of capital in one name
    max_positions: int = 20
    min_bucket: int = 4                       # defensive bucket 4-5 only (5 = most defensive)
    min_traded_value: float = 5e7             # Rs per day (median, 60 sessions)
    max_drawdown_from_high: float = -0.35     # exclude names more than 35% below their 120-session high
    adv_participation: float = 0.01           # never more than 1% of a day's traded value
    max_pair_corr: float = 0.55               # skip a name whose 250-session return correlation with an already-chosen name exceeds this
    horizon: str = "position_20d"
    holidays: frozenset = field(default_factory=frozenset)


def add_sessions(start: date, n: int, holidays=frozenset()) -> date:
    """The date n NSE sessions after `start` (weekends skipped; `holidays` skipped when known - future holidays are NOT known here,
    so a future date is an estimate and the card says so)."""
    d, k = start, 0
    while k < n:
        d += timedelta(days=1)
        if d.weekday() < 5 and d not in holidays:
            k += 1
    return d


def _sigma(close: pd.Series, span: int) -> float:
    r = close.pct_change().dropna()
    return float(r.ewm(span=span, min_periods=min(60, span)).std().iloc[-1]) if len(r) >= 60 else float("nan")


def build_cards(scores: pd.DataFrame, prices: pd.DataFrame, p: PortfolioParams = PortfolioParams(), venues: Optional[dict] = None) -> dict:
    """scores: latest defensive scores (symbol, date, defensive_score, risk_bucket, atr_pct, vol60, dd120, liq, warnings, regime).
    prices: symbol, date, close (adjusted), covering the candidates. Returns {"cards": [...], "summary": {...}}."""
    if not (0 < p.risk_per_position <= 0.05 and 0 < p.max_position <= 0.25 and p.capital > 0 and 1 <= p.max_positions <= 100):
        raise ValueError("portfolio parameters out of range")
    spec = get_horizon(p.horizon)
    as_of = pd.Timestamp(scores["date"].iloc[0]).date()
    regime = str(scores["regime"].iloc[0]) if "regime" in scores else "unknown"
    risk_mult, exposure_cap = REGIME_RISK_MULT.get(regime, 0.5), REGIME_EXPOSURE_CAP.get(regime, 0.5)
    entry_date = add_sessions(as_of, 1, p.holidays)
    exit_by = add_sessions(as_of, spec.label_horizon_days + 1, p.holidays)          # entry at the next open, then h sessions
    review_by = min(exit_by, add_sessions(entry_date, 5, p.holidays))

    elig = scores[(scores["risk_bucket"] >= p.min_bucket) & (scores["liq"] >= p.min_traded_value) & (scores["dd120"] >= p.max_drawdown_from_high)]
    elig = elig.sort_values(["defensive_score", "symbol"], ascending=[False, True])
    px = {s: g.sort_values("date") for s, g in prices[prices["date"] <= pd.Timestamp(as_of)].groupby("symbol")}
    cards, used, skipped = [], 0.0, []
    rets: dict = {}
    for r in elig.itertuples():
        if len(cards) >= p.max_positions:
            break
        g = px.get(r.symbol)
        if g is None or len(g) < 80:
            skipped.append((r.symbol, "not enough price history"))
            continue
        last = float(g["close"].iloc[-1])
        sig = _sigma(g["close"], spec.barrier.vol_span)
        if not (np.isfinite(sig) and sig > 0 and last > 0):
            skipped.append((r.symbol, "volatility unavailable"))
            continue
        rt = g.set_index("date")["close"].pct_change().dropna().tail(250)
        clash = next(((o, float(rt.corr(rets[o]))) for o in rets if len(rt.index.intersection(rets[o].index)) >= 120 and rt.corr(rets[o]) > p.max_pair_corr), None)
        if clash is not None:
            skipped.append((r.symbol, f"too correlated with {clash[0]} ({clash[1]:.2f}): same risk twice"))
            continue
        stop_dist = spec.barrier.sl_mult * sig
        by_risk = p.capital * p.risk_per_position * risk_mult / stop_dist
        by_cap = p.capital * p.max_position
        by_adv = p.adv_participation * float(r.liq)
        value = min(by_risk, by_cap, by_adv)
        room = p.capital * exposure_cap - used
        if room <= 0:
            skipped.append((r.symbol, "regime exposure cap reached"))
            break
        value = min(value, room)
        qty = int(np.floor(value / last))
        if qty < 1:
            skipped.append((r.symbol, "position rounds to zero shares"))
            continue
        pos_val = qty * last
        stop = last * (1.0 - stop_dist)
        binding = "risk budget" if by_risk <= min(by_cap, by_adv, room) else "max position" if by_cap <= min(by_adv, room) else "liquidity" if by_adv <= room else "exposure cap"
        used += pos_val
        rets[r.symbol] = rt
        cards.append({
            "symbol": r.symbol, "instrument_type": "equity", "venues": (venues or {}).get(r.symbol, ["NSE"]),
            "defensive_score": float(r.defensive_score), "risk_bucket": int(r.risk_bucket),
            "last_close": last, "daily_vol": sig, "quantity": qty, "value": pos_val, "weight": pos_val / p.capital,
            "entry": "next session open", "entry_date": entry_date.isoformat(), "stop": stop, "stop_distance": stop_dist,
            "loss_if_stopped": qty * last * stop_dist, "loss_pct_of_capital": qty * last * stop_dist / p.capital,
            "sized_by": binding, "review_by": review_by.isoformat(), "exit_by": exit_by.isoformat(),
            "triggers": ["close below the stop (a gap through fills at the open, not at the stop)",
                         "universe regime turns risk-off: cut to the risk-off exposure cap",
                         "defensive bucket falls below 3: review", f"time exit after {spec.label_horizon_days} sessions"],
            "reasons": [f"defensive bucket {int(r.risk_bucket)} of 5 (score {float(r.defensive_score):.2f})", f"60d volatility {float(r.vol60):.1%}",
                        f"{float(r.dd120):.0%} from its 120-session high", f"median traded value Rs {float(r.liq) / 1e7:.0f} Cr/day"],
            "warnings": [w for w in str(r.warnings).split("; ") if w],
            "evidence": EVIDENCE,
        })
    total_loss = sum(c["loss_if_stopped"] for c in cards)
    names = [c["symbol"] for c in cards]
    if len(names) >= 2:
        cm = pd.concat([rets[n] for n in names], axis=1, keys=names).dropna().corr().to_numpy()
        avg_corr = float(cm[np.triu_indices(len(names), 1)].mean())
    else:
        avg_corr = None
    summary = {
        "as_of": as_of.isoformat(), "regime": regime, "regime_risk_multiplier": risk_mult, "exposure_cap": exposure_cap,
        "horizon": p.horizon, "horizon_sessions": spec.label_horizon_days, "entry_date": entry_date.isoformat(), "exit_by": exit_by.isoformat(),
        "dates_are_estimates": True, "capital": p.capital, "positions": len(cards), "invested": used, "invested_pct": used / p.capital,
        "cash": p.capital - used, "largest_weight": max((c["weight"] for c in cards), default=0.0),
        "average_pairwise_correlation": avg_corr, "loss_if_all_stopped": total_loss, "loss_if_all_stopped_pct": total_loss / p.capital,
        "candidates_considered": int(len(elig)), "skipped": skipped[:20],
        "evidence": EVIDENCE,
        "notes": ["Risk-based allocation, not a forecast: no model has shown a net edge.",
                  "Stops are not guaranteed fills; a gap through the stop fills at the open.",
                  "Dates assume weekday sessions; exchange holidays after today are not known here.",
                  f"Concentration is limited by return correlation (no name above {p.max_pair_corr:.2f} with another), not by sector: there is no sector data yet.",
                  "Only NSE data is loaded today; BSE-only listings are not covered yet."]}
    return {"cards": cards, "summary": summary}
