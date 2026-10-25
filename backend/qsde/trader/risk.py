"""Trader risk engine (pure): position sizing and the hard limits, enforced in code. The same functions are used to PROPOSE an idea and to PERSIST it, so a direct API call cannot skip a limit.

Definitions (stated once, used everywhere):
  loss per unit at the stop = |entry - stop| + round-trip cost per unit + gap allowance
      cost per unit    = round-trip cost fraction (the paper default of the idea's horizon) x entry
      gap allowance    = the 90th percentile of the last 60 sessions' absolute overnight gaps x entry (a stop is not a guarantee: price can open beyond it)
  risk of an idea (rupees)  = quantity x loss per unit at the stop
  size = floor( risk budget / loss per unit ), then capped by liquidity, free cash in the trading pot and the remaining open-risk budget, then rounded down to the lot size
      risk budget = trading capital x risk per trade % x size multiplier        (trading capital only: never investment money)
  daily P&L  = realised P&L of trades closed today + the change since the previous close in the value of open positions
  weekly P&L = the same, since the first session of the week
  size multiplier = 0.5 while a losing-streak reduction is active, times 0.5 in the week after a weekly-limit breach (both can apply: 0.25)
Refusals never guess: every one names the rule, the observed value and the limit.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

STREAK_STOPS = 3                      # consecutive stop-outs that trigger a size reduction
STREAK_REDUCED_TRADES = 5             # the next trades opened are half size
REDUCTION = 0.5
STALE_SESSIONS = 1                    # prices older than this many sessions behind the expected last session are refused
EVIDENCE = ("RESEARCH", "PAPER", "APPROVED")


@dataclass(frozen=True)
class Limits:
    risk_per_trade_pct: float
    open_risk_pct: float
    daily_loss_pct: float
    weekly_loss_pct: float
    max_positions: int
    max_pair_correlation: float
    liquidity_cap_pct: float

    @classmethod
    def from_effective(cls, e: dict) -> "Limits":
        return cls(float(e["risk_per_trade_pct"]), float(e["open_risk_pct"]), float(e["daily_loss_pct"]), float(e["weekly_loss_pct"]), int(e["max_positions"]),
                   float(e["max_pair_correlation"]), float(e["liquidity_cap_pct"]))


@dataclass
class OpenPosition:
    symbol: str
    qty: int
    entry: float
    stop: float
    last_price: float
    prev_close: float
    risk_rupees: float                # stored at entry: qty x loss per unit at the stop


@dataclass
class TradeRecord:
    symbol: str
    opened_at: datetime
    closed_at: Optional[datetime] = None
    exit_reason: Optional[str] = None          # stop | target | time | manual
    pnl_rupees: float = 0.0


@dataclass
class State:
    capital: float                                        # the TRADING pot
    open_positions: list = field(default_factory=list)
    realised_today: float = 0.0
    realised_week: float = 0.0
    week_start_value_change: float = 0.0                  # change in open-position value since the week's first session (caller computes)
    trades: list = field(default_factory=list)            # TradeRecord, any order
    previous_week_pnl: float = 0.0                        # realised + unrealised change of the previous week
    price_sessions_behind: int = 0                        # how many sessions the latest price lags the expected last session
    unprotected: list = field(default_factory=list)       # symbols of imported trading-pot positions that have no recorded stop
    book: list = field(default_factory=list)              # Section A rows: every open position (paper ideas and imported) with its command; display only, never read by the gate


def open_risk(s: State) -> float:
    return float(sum(p.risk_rupees for p in s.open_positions))


def invested(s: State) -> float:
    return float(sum(p.qty * p.last_price for p in s.open_positions))


def unrealised_today(s: State) -> float:
    return float(sum(p.qty * (p.last_price - p.prev_close) for p in s.open_positions))


def daily_pnl(s: State) -> float:
    return s.realised_today + unrealised_today(s)


def weekly_pnl(s: State) -> float:
    return s.realised_week + s.week_start_value_change


def streak_status(trades: list) -> dict:
    """{stops_in_row, triggered_at, reduced_trades_left}. A trigger is the closing of the 3rd consecutive stop-out; the next 5 trades OPENED after it are reduced."""
    closed = sorted([t for t in trades if t.closed_at is not None], key=lambda t: t.closed_at)
    run, trigger = 0, None
    for t in closed:
        if t.exit_reason == "stop":
            run += 1
            if run >= STREAK_STOPS:
                trigger = t.closed_at
        else:
            run = 0
    left = 0
    if trigger is not None:
        used = sum(1 for t in trades if t.opened_at > trigger)
        left = max(0, STREAK_REDUCED_TRADES - used)
    return {"stops_in_row": run, "triggered_at": trigger, "reduced_trades_left": left}


def size_multiplier(s: State, l: Limits) -> tuple[float, list]:
    reasons, m = [], 1.0
    st = streak_status(s.trades)
    if st["reduced_trades_left"] > 0:
        m *= REDUCTION
        reasons.append(f"losing streak: {STREAK_STOPS} stops in a row, so the next {st['reduced_trades_left']} trade(s) are half size")
    if s.previous_week_pnl <= -l.weekly_loss_pct / 100.0 * s.capital:
        m *= REDUCTION
        reasons.append(f"last week lost {-s.previous_week_pnl / s.capital:.1%} of trading capital (limit {l.weekly_loss_pct:g}%), so this week's size is halved")
    return m, reasons


@dataclass
class Sizing:
    qty: int
    loss_per_unit: float
    risk_rupees: float
    risk_pct: float
    position_value: float
    binding: str
    multiplier: float
    detail: dict = field(default_factory=dict)


def size_idea(capital: float, l: Limits, mult: float, entry: float, stop: float, cost_per_unit: float, gap_allowance: float, adv_value: float, free_cash: float,
              open_risk_left: float, lot_size: int = 1) -> Sizing:
    stop_dist = abs(entry - stop)
    loss_per_unit = stop_dist + cost_per_unit + gap_allowance
    budget = capital * l.risk_per_trade_pct / 100.0 * mult
    caps = {"risk_per_trade": budget / loss_per_unit if loss_per_unit > 0 else 0.0,
            "liquidity": (l.liquidity_cap_pct / 100.0 * adv_value / entry) if entry > 0 and adv_value > 0 else 0.0,
            "free_cash": free_cash / entry if entry > 0 else 0.0,
            "open_risk_cap": open_risk_left / loss_per_unit if loss_per_unit > 0 else 0.0}
    binding = min(caps, key=caps.get)
    raw = max(0, math.floor(caps[binding]))
    qty = (raw // lot_size) * lot_size if lot_size > 1 else raw
    risk = qty * loss_per_unit
    return Sizing(qty, loss_per_unit, risk, risk / capital if capital else 0.0, qty * entry, binding, mult,
                  {"risk_budget": budget, "caps_in_units": {k: round(v, 2) for k, v in caps.items()}, "stop_distance": stop_dist, "cost_per_unit": cost_per_unit, "gap_allowance": gap_allowance})


@dataclass
class Decision:
    allowed: bool
    sizing: Optional[Sizing]
    reasons: list = field(default_factory=list)            # why refused (each names rule, value, limit)
    notes: list = field(default_factory=list)              # size reductions and warnings that did not stop the idea


def evaluate(s: State, l: Limits, *, symbol: str, side: str, entry: float, stop: float, cost_per_unit: float, gap_allowance: float, adv_value: float,
             correlations: Optional[dict] = None, lot_size: int = 1, evidence: str = "RESEARCH", engine_approved: bool = False, instrument: str = "cash") -> Decision:
    """The single gate. Returns a Decision; an idea that is refused here must never be stored or shown as a card."""
    why: list = []
    if evidence not in EVIDENCE:
        why.append(f"evidence label must be one of {', '.join(EVIDENCE)}")
    elif evidence == "APPROVED" and not engine_approved:
        why.append("evidence APPROVED is not available: no engine is approved (ideas are RESEARCH or PAPER)")
    if instrument != "cash":
        why.append(f"instrument '{instrument}': futures and options are research and paper only, defined-risk structures only, and not available in this engine yet")
    if side != "long":
        why.append("only long cash ideas are supported: short selling and naked short options are not offered")
    if not s.capital or s.capital <= 0:
        why.append("trading capital is not set: enter your trading pot on the Profile page first (nothing is assumed)")
    if not (entry > 0 and stop > 0):
        why.append("entry and stop must be positive prices")
    elif side == "long" and stop >= entry:
        why.append(f"stop {stop:g} must be below the entry {entry:g} for a long idea")
    if s.unprotected:
        why.append(f"{len(s.unprotected)} imported trading-pot position(s) have no stop ({', '.join(sorted(s.unprotected))}): record a stop for each first, because the existing book is managed before any new idea")
    if s.price_sessions_behind > STALE_SESSIONS:
        why.append(f"price data is {s.price_sessions_behind} sessions behind: refused until the data is current")
    if why:
        return Decision(False, None, why)
    dpl, wpl = daily_pnl(s), weekly_pnl(s)
    if dpl <= -l.daily_loss_pct / 100.0 * s.capital:
        why.append(f"daily loss limit reached: {dpl / s.capital:.2%} of trading capital against a limit of -{l.daily_loss_pct:g}%; no new ideas today")
    if len(s.open_positions) >= l.max_positions:
        why.append(f"{len(s.open_positions)} positions are open; the maximum is {l.max_positions}")
    for p in s.open_positions:
        if p.symbol == symbol:
            why.append(f"{symbol} is already open" + (f" and the price ({entry:g}) is below your entry ({p.entry:g}): averaging down is not offered" if entry < p.entry else ""))
    for sym, c in (correlations or {}).items():
        if c is not None and c > l.max_pair_correlation:
            why.append(f"correlation with open position {sym} is {c:.2f}; the maximum is {l.max_pair_correlation:g}")
    mult, notes = size_multiplier(s, l)
    left = l.open_risk_pct / 100.0 * s.capital - open_risk(s)
    if left <= 0:
        why.append(f"open risk {open_risk(s) / s.capital:.2%} is at the cap of {l.open_risk_pct:g}% of trading capital")
    free = max(0.0, s.capital - invested(s))
    if why:
        return Decision(False, None, why, notes)
    z = size_idea(s.capital, l, mult, entry, stop, cost_per_unit, gap_allowance, adv_value, free, left, lot_size)
    if z.qty < 1:
        why.append(f"not even one {'lot' if lot_size > 1 else 'share'} fits: the binding limit is '{z.binding}' ({z.detail['caps_in_units']})")
        return Decision(False, None, why, notes)
    if z.binding != "risk_per_trade":
        notes.append(f"size is capped by '{z.binding}' (below the per-trade risk budget)")
    wk = wpl / s.capital
    if wpl <= -l.weekly_loss_pct / 100.0 * s.capital:
        notes.append(f"weekly loss limit reached ({wk:.2%} against -{l.weekly_loss_pct:g}%): sizing halves next week")
    return Decision(True, z, [], notes)


def meters(s: State, l: Limits) -> dict:
    """What the desk shows: how much of each limit is used."""
    cap = s.capital or 0.0
    div = (lambda a, b: (a / b) if b else 0.0)
    m, why = size_multiplier(s, l) if cap else (1.0, [])
    st = streak_status(s.trades)
    return {"capital": cap, "open_risk": open_risk(s), "open_risk_pct": div(open_risk(s), cap) * 100, "open_risk_limit_pct": l.open_risk_pct,
            "daily_pnl": daily_pnl(s), "daily_pnl_pct": div(daily_pnl(s), cap) * 100, "daily_loss_limit_pct": l.daily_loss_pct,
            "daily_limit_used": max(0.0, div(-daily_pnl(s), l.daily_loss_pct / 100.0 * cap)) if cap else 0.0,
            "weekly_pnl": weekly_pnl(s), "weekly_pnl_pct": div(weekly_pnl(s), cap) * 100, "weekly_loss_limit_pct": l.weekly_loss_pct,
            "weekly_limit_used": max(0.0, div(-weekly_pnl(s), l.weekly_loss_pct / 100.0 * cap)) if cap else 0.0,
            "positions": len(s.open_positions), "max_positions": l.max_positions, "size_multiplier": m, "size_multiplier_reasons": why, "streak": st,
            "invested": invested(s), "free_cash": max(0.0, cap - invested(s)), "unprotected": sorted(s.unprotected)}
