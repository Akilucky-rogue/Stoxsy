"""Existing book first. Positions the owner already holds in the TRADING pot (the latest trading-pot import) are part of the risk state before any new idea is offered.

A legacy position is PROTECTED once the owner has recorded a stop for it (`legacy_triage`); until then it is UNPROTECTED: its risk is unbounded, so it is counted at an emergency stop
(the lower of the 5-session swing low and last - 2 x ATR14) and new ideas are refused until it has a stop. Investment-pot imports are never read here: a trade cannot migrate between pots.

`manage` is the Section A rule: one plain command per open position, from the same fields for imported positions and logged paper ideas.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

import numpy as np
import pandas as pd

TRAIL_AT_R = 1.5                       # once price is this many R above cost, the stop moves up to cost


def atr14(px: pd.DataFrame) -> Optional[float]:
    d = px.sort_values("date").tail(15)
    if len(d) < 15:
        return None
    pc = d["close"].shift(1)
    tr = np.maximum.reduce([(d["high"] - d["low"]).to_numpy(), (d["high"] - pc).abs().to_numpy(), (d["low"] - pc).abs().to_numpy()])
    return float(np.nanmean(tr[1:]))


def emergency_stop(px: pd.DataFrame) -> Optional[float]:
    """min(lowest low of the last 5 sessions, last close - 2 x ATR14); None when there is too little history."""
    d = px.sort_values("date")
    a = atr14(d)
    if a is None or len(d) < 5:
        return None
    return round(float(min(d["low"].tail(5).min(), d["close"].iloc[-1] - 2.0 * a)), 2)


def manage(*, entry: float, stop: Optional[float], last: float, time_stop: Optional[date] = None, today: Optional[date] = None) -> dict:
    """Section A command for one open long position. Order matters: a breached stop outranks everything."""
    today = today or date.today()
    if stop is None:
        return {"action": "SET_STOP_NOW", "why": "no stop is recorded: the risk of this position is unbounded"}
    if last <= stop:
        return {"action": "EXIT_STOP_BREACHED", "why": f"last close {last:g} is at or below the stop {stop:g}"}
    if time_stop is not None and time_stop <= today:
        return {"action": "EXIT_TIME_STOP", "why": f"the time stop ({time_stop}) has arrived"}
    r = entry - stop
    if r > 0 and stop < entry and last >= entry + TRAIL_AT_R * r:
        return {"action": "RAISE_STOP_TO_COST", "why": f"price is {(last - entry) / r:.1f}R above cost: move the stop up to {entry:g} so the trade cannot lose"}
    return {"action": "HOLD_AS_PLANNED", "why": "inside its stop, before its time stop"}


def latest_trading_imports(read_sql, user_id: str) -> list:
    """The latest trading-pot import of each broker account (an import without an account label counts as its own group): two accounts both count, a newer import replaces only its own account's older one."""
    d = read_sql("SELECT DISTINCT ON (coalesce(broker_account, '')) id FROM portfolio_imports WHERE user_id = :u AND pot = 'trading' "
                 "ORDER BY coalesce(broker_account, ''), imported_at DESC, id DESC", params={"u": user_id})
    return [int(x) for x in d["id"]]


def untriaged(read_sql, user_id: str) -> list:
    """Symbols held in a broker account's latest snapshot that the owner has not yet assigned to the investment or trading pot. They are not in either pot, so nothing can say how they are managed:
    new ideas stay locked until each is assigned."""
    d = read_sql("WITH latest AS (SELECT DISTINCT ON (broker_account) id FROM portfolio_imports WHERE user_id = :u AND broker_account IS NOT NULL AND pot IS NULL "
                 "ORDER BY broker_account, imported_at DESC, id DESC) SELECT DISTINCT h.symbol FROM portfolio_holdings h JOIN latest l ON l.id = h.import_id ORDER BY 1", params={"u": user_id})
    return d["symbol"].tolist()


def positions(read_sql, user_id: str, load_prices, since: date, gap_fraction=None) -> list:
    """One dict per holding of the latest trading-pot import(s): quantity, average cost, last and previous close, recorded stop (or the emergency stop it is counted at), risk, protection, command."""
    imps = latest_trading_imports(read_sql, user_id)
    if not imps:
        return []
    h = read_sql("SELECT h.import_id, h.symbol, h.quantity::float8 AS qty, h.avg_cost::float8 AS avg_cost, t.stop_price::float8 AS stop, t.horizon_id FROM portfolio_holdings h "
                 "LEFT JOIN legacy_triage t ON t.import_id = h.import_id AND t.symbol = h.symbol WHERE h.import_id = ANY(:i) ORDER BY h.symbol, h.import_id", params={"i": imps})
    if h.empty:
        return []
    px = load_prices(read_sql, h["symbol"].unique().tolist(), since)
    out = []
    for r in h.itertuples():
        imp = int(r.import_id)
        s = px[px["symbol"] == r.symbol]
        if s.empty:
            out.append({"import_id": imp, "symbol": r.symbol, "qty": r.qty, "avg_cost": r.avg_cost, "last": None, "prev_close": None, "stop": r.stop, "emergency_stop": None, "counted_stop": None,
                        "protected": not pd.isna(r.stop), "risk_rupees": None, "horizon_id": r.horizon_id, "action": "NO_PRICE", "why": "no price history for this symbol"})
            continue
        s = s.sort_values("date")
        last, prev = float(s["close"].iloc[-1]), float(s["close"].iloc[-2]) if len(s) > 1 else float(s["close"].iloc[-1])
        stop = None if pd.isna(r.stop) else float(r.stop)
        emerg = emergency_stop(s)
        counted = stop if stop is not None else (emerg if emerg is not None else last * 0.9)
        gap = (gap_fraction(s) * last) if gap_fraction else 0.0
        risk = r.qty * (max(0.0, last - counted) + gap)
        m = manage(entry=r.avg_cost, stop=stop, last=last)
        out.append({"import_id": imp, "symbol": r.symbol, "qty": r.qty, "avg_cost": r.avg_cost, "last": last, "prev_close": prev, "stop": stop, "emergency_stop": emerg, "counted_stop": counted,
                    "protected": stop is not None, "risk_rupees": round(risk, 2), "horizon_id": r.horizon_id, **m})
    return out
