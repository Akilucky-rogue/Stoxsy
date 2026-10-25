"""Trader state from the database, and the idea lifecycle (propose, persist, close). The gate in `risk.evaluate` is called on EVERY path that stores an idea, with state read from the
database here, so no caller can skip a limit by posting a prepared card.

Prices come from the official `ohlcv` series. Weekly P&L uses, for a position opened before the week, the last close before the week as its base and, for one opened inside it, the entry price.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import numpy as np
import pandas as pd
import psycopg2.extras

from qsde.alerts import rules as alert_rules
from qsde.modes import policy as pol
from qsde.risk import costs
from qsde.trader import risk as R

IST = "Asia/Kolkata"


def week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


def load_prices(read_sql, symbols: list, since: date) -> pd.DataFrame:
    if not symbols:
        return pd.DataFrame(columns=["symbol", "date", "open", "high", "low", "close", "volume"])
    d = read_sql("SELECT symbol, date, open, high, low, close, volume FROM ohlcv WHERE symbol = ANY(:s) AND date >= CAST(:d AS DATE) ORDER BY symbol, date", params={"s": symbols, "d": str(since)})
    d["date"] = pd.to_datetime(d["date"])
    return d


def last_close_on_or_before(px: pd.DataFrame, symbol: str, day: date) -> Optional[float]:
    d = px[(px["symbol"] == symbol) & (px["date"] <= pd.Timestamp(day))]
    return None if d.empty else float(d["close"].iloc[-1])


def sessions_behind(read_sql, now: pd.Timestamp) -> int:
    last = read_sql("SELECT max(date) AS d FROM ohlcv")["d"].iloc[0]
    exp = alert_rules.expected_last_session(now.tz_convert(IST), 20)
    return alert_rules.weekdays_behind(pd.Timestamp(last).date(), exp)


def build_state(read_sql, user_id: str, capital: float, now: Optional[pd.Timestamp] = None, ideas: Optional[pd.DataFrame] = None) -> tuple[R.State, pd.DataFrame]:
    now = now or pd.Timestamp.now(tz="UTC")
    today = now.tz_convert(IST).date()
    ws = week_start(today)
    pws = ws - timedelta(days=7)
    if ideas is None:
        ideas = read_sql("SELECT * FROM trader_ideas WHERE user_id = :u ORDER BY opened_at", params={"u": user_id})
    syms = sorted(ideas["symbol"].unique().tolist()) if len(ideas) else []
    px = load_prices(read_sql, syms, pws - timedelta(days=10))
    opens, trades = [], []
    realised_today = realised_week = prev_week_realised = 0.0
    wk_change = prev_wk_unreal = 0.0
    for r in ideas.itertuples():
        opened = pd.Timestamp(r.opened_at)
        closed = None if pd.isna(r.closed_at) else pd.Timestamp(r.closed_at)
        trades.append(R.TradeRecord(r.symbol, opened.to_pydatetime(), None if closed is None else closed.to_pydatetime(), None if pd.isna(r.exit_reason) else r.exit_reason,
                                    0.0 if pd.isna(r.pnl_rupees) else float(r.pnl_rupees)))
        if closed is not None:
            cd = closed.tz_convert(IST).date()
            if cd == today:
                realised_today += float(r.pnl_rupees)
            if cd >= ws:
                realised_week += float(r.pnl_rupees)
            elif pws <= cd < ws:
                prev_week_realised += float(r.pnl_rupees)
        s = px[px["symbol"] == r.symbol]
        if closed is None:
            last = float(s["close"].iloc[-1]) if len(s) else float(r.entry)
            prev = float(s["close"].iloc[-2]) if len(s) > 1 else last
            opens.append(R.OpenPosition(r.symbol, int(r.qty), float(r.entry), float(r.stop), last, prev, float(r.risk_rupees)))
            od = opened.tz_convert(IST).date()
            base = float(r.entry) if od >= ws else (last_close_on_or_before(px, r.symbol, ws - timedelta(days=1)) or float(r.entry))
            wk_change += int(r.qty) * (last - base)
        # previous week's unrealised change on positions that were open at its end
        od = opened.tz_convert(IST).date()
        if od < ws and (closed is None or closed.tz_convert(IST).date() >= ws):
            end = last_close_on_or_before(px, r.symbol, ws - timedelta(days=1))
            base = float(r.entry) if od >= pws else (last_close_on_or_before(px, r.symbol, pws - timedelta(days=1)) or float(r.entry))
            if end is not None:
                prev_wk_unreal += int(r.qty) * (end - base)
    st = R.State(capital=float(capital or 0.0), open_positions=opens, realised_today=realised_today, realised_week=realised_week, week_start_value_change=wk_change, trades=trades,
                 previous_week_pnl=prev_week_realised + prev_wk_unreal, price_sessions_behind=sessions_behind(read_sql, now))
    return st, px


def gap_allowance_fraction(px_sym: pd.DataFrame, lookback: int = 60) -> float:
    d = px_sym.sort_values("date").tail(lookback + 1)
    if len(d) < 20:
        return 0.02                                    # too little history: a deliberately wide default (2%), never zero
    gap = (d["open"].to_numpy()[1:] / d["close"].to_numpy()[:-1] - 1.0)
    return float(np.nanpercentile(np.abs(gap), 90))


def adv_value(px_sym: pd.DataFrame, window: int = 20) -> float:
    d = px_sym.sort_values("date").tail(window)
    return float((d["close"] * d["volume"]).median()) if len(d) else 0.0


def correlations(read_sql, symbol: str, open_symbols: list, sessions: int = 60) -> dict:
    if not open_symbols:
        return {}
    d = read_sql("SELECT symbol, date, close FROM ohlcv WHERE symbol = ANY(:s) AND date >= CURRENT_DATE - 140 ORDER BY date", params={"s": [symbol, *open_symbols]})
    if d.empty or symbol not in set(d["symbol"]):
        return {}
    w = d.pivot(index="date", columns="symbol", values="close").sort_index().pct_change().tail(sessions)
    out = {}
    for o in open_symbols:
        if o in w.columns and symbol in w.columns and w[[o, symbol]].dropna().shape[0] >= 30:
            out[o] = float(w[o].corr(w[symbol]))
    return out


def horizon_params(horizon_id: str) -> dict:
    from qsde.ml.registry import get_horizon

    h = get_horizon(horizon_id)
    return {"sessions": h.label_horizon_days, "stop_mult": h.barrier.sl_mult, "target_mult": h.barrier.pt_mult, "vol_span": h.barrier.vol_span, "legacy": h.legacy}


def add_sessions(start: date, n: int) -> date:
    d = start
    while n > 0:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


def propose(read_sql, user_id: str, profile: dict, *, symbol: str, horizon_id: str, setup: str, entry: Optional[float] = None, stop: Optional[float] = None, target: Optional[float] = None,
            evidence: str = "RESEARCH", now: Optional[pd.Timestamp] = None, instrument: str = "cash", side: str = "long", state: Optional[R.State] = None) -> dict:
    """Run the gate for one idea against the user's real state. Returns {allowed, card|None, reasons, notes, meters}. Nothing is stored."""
    now = now or pd.Timestamp.now(tz="UTC")
    l = R.Limits.from_effective(pol.effective_limits(profile.get("limits")))
    cap = float(profile.get("trading_capital") or 0.0)
    st = state if state is not None else build_state(read_sql, user_id, cap, now)[0]
    px = load_prices(read_sql, [symbol], now.date() - timedelta(days=260))
    px_sym = px[px["symbol"] == symbol]
    base = {"meters": R.meters(st, l), "limits": l.__dict__}
    if px_sym.empty:
        return {**base, "allowed": False, "card": None, "reasons": [f"no price history for {symbol}"], "notes": []}
    last = float(px_sym["close"].iloc[-1])
    hp = horizon_params(horizon_id)
    sig = float(px_sym["close"].pct_change().tail(hp["vol_span"]).ewm(span=hp["vol_span"]).std().iloc[-1]) if len(px_sym) > 30 else 0.0
    e = float(entry) if entry else last
    s = float(stop) if stop else e * (1 - hp["stop_mult"] * sig)
    t = float(target) if target else e * (1 + hp["target_mult"] * sig)
    cost = costs.cost_frac(hp["legacy"], paper_default=True) * e
    gap = gap_allowance_fraction(px_sym) * e
    corr = correlations(read_sql, symbol, [p.symbol for p in st.open_positions])
    d = R.evaluate(st, l, symbol=symbol, side=side, entry=e, stop=s, cost_per_unit=cost, gap_allowance=gap, adv_value=adv_value(px_sym), correlations=corr, evidence=evidence,
                      engine_approved=pol.engine_approved(), instrument=instrument)
    if not d.allowed:
        return {**base, "allowed": False, "card": None, "reasons": d.reasons, "notes": d.notes}
    z = d.sizing
    risk_per_share = e - s
    card = {"symbol": symbol, "venue": "NSE cash", "setup": setup, "direction": "long", "horizon_id": horizon_id, "evidence": evidence,
            "evidence_note": ("RESEARCH: a candidate from a rule baseline. No engine is approved and no edge has been shown; the resolved sample for this setup is 0 until it is logged and resolved." if evidence == "RESEARCH"
                              else "PAPER: tracked on paper; not approved."),
            "entry": round(e, 2), "entry_rule": "buy at or just below the planned entry at the next session's open; skip the idea if it opens more than half a stop-distance above the entry",
            "stop": round(s, 2), "stop_pct": round((e - s) / e, 4), "stop_in_daily_moves": round((e - s) / e / sig, 2) if sig else None,
            "target": round(t, 2), "target_r": round((t - e) / risk_per_share, 2) if risk_per_share > 0 else None, "time_stop": str(add_sessions(now.tz_convert(IST).date(), hp["sessions"])),
            "till_when": f"exit by the time stop ({hp['sessions']} sessions) if neither the stop nor the target is hit",
            "qty": z.qty, "position_value": round(z.position_value, 2), "risk_rupees": round(z.risk_rupees, 2), "risk_pct_of_trading_capital": round(z.risk_pct * 100, 3),
            "expected_cost_rupees": round(z.qty * cost, 2), "loss_per_unit": round(z.loss_per_unit, 2), "sizing": {"binding_limit": z.binding, "multiplier": z.multiplier, **z.detail},
            "invalidated_if": [f"the price closes below {s:.2f}", "the open gaps below the stop (the loss is then larger than planned)", f"the idea is still open after {hp['sessions']} sessions"],
            "limits": l.__dict__, "as_of": str(pd.Timestamp(px_sym["date"].iloc[-1]).date()), "data_through": str(pd.Timestamp(px_sym["date"].iloc[-1]).date())}
    return {**base, "allowed": True, "card": card, "reasons": [], "notes": d.notes}


def persist(conn, user_id: str, decision: dict, role: str) -> int:
    """Store a card that passed the gate. The caller must have produced `decision` from `propose` moments ago; the row's own CHECK constraints are the last line of defence."""
    from qsde.modes import profile as prof

    if not decision["allowed"] or decision["card"] is None:
        raise ValueError("a refused idea cannot be stored")
    c = decision["card"]
    cur = conn.cursor()
    cur.execute("INSERT INTO trader_ideas (user_id, symbol, setup, side, horizon_id, evidence, entry, stop, target, qty, risk_rupees, time_stop, card) VALUES (%s,%s,%s,'long',%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                (user_id, c["symbol"], c["setup"], c["horizon_id"], c["evidence"], c["entry"], c["stop"], c["target"], c["qty"], c["risk_rupees"], c["time_stop"], psycopg2.extras.Json(c)))
    iid = int(cur.fetchone()["id"])
    prof.record(conn, user_id, role, "trader.idea", {"id": iid, "symbol": c["symbol"], "qty": c["qty"], "risk_rupees": c["risk_rupees"], "evidence": c["evidence"]})
    return iid


def close(conn, user_id: str, role: str, idea_id: int, price: float, reason: str, notes: Optional[str] = None) -> dict:
    from qsde.modes import profile as prof

    cur = conn.cursor()
    cur.execute("SELECT * FROM trader_ideas WHERE id = %s AND user_id = %s AND status = 'open' FOR UPDATE", (idea_id, user_id))
    r = cur.fetchone()
    if r is None:
        raise LookupError("open idea not found")
    qty, entry, risk = int(r["qty"]), float(r["entry"]), float(r["risk_rupees"])
    pnl = qty * (price - entry)
    rmult = pnl / risk if risk else None
    cur.execute("UPDATE trader_ideas SET status = 'closed', closed_at = NOW(), exit_price = %s, exit_reason = %s, pnl_rupees = %s, r_multiple = %s, notes = COALESCE(%s, notes) WHERE id = %s",
                (price, reason, round(pnl, 2), None if rmult is None else round(rmult, 4), notes, idea_id))
    prof.record(conn, user_id, role, "trader.close", {"id": idea_id, "price": price, "reason": reason, "pnl_rupees": round(pnl, 2), "r": rmult})
    return {"id": idea_id, "pnl_rupees": round(pnl, 2), "r_multiple": rmult}


def performance(read_sql, user_id: str) -> dict:
    d = read_sql("SELECT pnl_rupees, r_multiple, exit_reason, evidence FROM trader_ideas WHERE user_id = :u AND status = 'closed'", params={"u": user_id})
    n = len(d)
    if n == 0:
        return {"n_closed": 0, "note": "no closed ideas yet: nothing to measure", "min_for_judgement": 60}
    r = d["r_multiple"].astype(float)
    return {"n_closed": n, "hit_rate": float((r > 0).mean()), "expectancy_r": float(r.mean()), "median_r": float(r.median()), "total_pnl_rupees": float(d["pnl_rupees"].astype(float).sum()),
            "by_exit": d["exit_reason"].value_counts().to_dict(), "min_for_judgement": 60,
            "note": (f"{n} closed ideas: too few to judge an engine (the written criteria need at least 60 resolved paper ideas)." if n < 60 else "enough closed ideas to start judging against the written criteria.")}
