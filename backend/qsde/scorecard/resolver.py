"""
Scorecard outcome resolver (Phase 0 increment 2).

Mechanically matches each logged TradeIdea to its realized barrier outcome so the
Scorecard reports the TRUE per-engine net-of-cost edge. Cash/future ideas are
walked forward through daily OHLCV against their target/stop/time barriers,
REUSING paper_journal._resolve_barrier_daily (the tested barrier walker — no
duplication). Option structures resolve at expiry (F&O engine, later) and are
skipped here.

The match is MECHANICAL: every OPEN idea whose window has elapsed is resolved,
none are chosen by hand — which, together with the emit-time idea-log, is what
keeps the Scorecard honest.
"""
from __future__ import annotations

import json
import logging
from datetime import date
from typing import Callable, Optional

import pandas as pd

from qsde.db.connection import execute_sql, read_sql
from qsde.execution.paper_journal import _resolve_barrier_daily
from qsde.fno.payoff import (
    condor_params_from_legs,
    estimate_condor_cost,
    iron_condor_settlement_pnl,
)
from qsde.risk.costs import cost_frac

log = logging.getLogger(__name__)

# Idea horizon -> barrier window in NSE sessions.
from qsde.ml.registry import legacy_sessions

_HORIZON_SESSIONS = legacy_sessions(("intraday", "swing", "rotation", "long"))   # registry-derived

# (underlying, expiry) -> settlement price, or None if not yet knowable.
SettlementLookup = Callable[[str, date], Optional[float]]


def resolve_idea_outcome(entry, stop, target, direction, horizon,
                         bars: Optional[pd.DataFrame]) -> Optional[dict]:
    """Pure: idea levels + forward daily bars -> a barrier-outcome dict, or None
    if there isn't enough forward data yet.

    ENTRY CONVENTION (same as the harness labels and the paper journal): the idea is decided at the close of
    its day (`entry` is that reference close) and FILLED at the open of the first bar in `bars`; stop and
    target are re-anchored to the fill with the idea's own fractional offsets
    (qsde.ml.barrier.levels_at_fill). Returns are measured from the fill. A first bar without a usable
    open cannot be filled -> None (the idea stays open rather than being graded on an unattainable price).

    bars: columns date/open/high/low/close, ascending. Reuses the tested barrier walker
    (pessimistic same-bar tie = stop wins).
    """
    from qsde.ml.barrier import levels_at_fill

    sessions = _HORIZON_SESSIONS.get(str(horizon).lower(), 5)
    if bars is None or bars.empty or len(bars) < sessions:
        return None
    window = bars.head(sessions)
    if "open" not in window.columns:
        log.warning("resolve_idea_outcome: bars carry no open column; cannot apply the next-open fill")
        return None
    fill = float(window.iloc[0]["open"]) if pd.notna(window.iloc[0]["open"]) else float("nan")
    if not (fill > 0):
        return None
    stop_f, target_f = levels_at_fill(float(entry), fill,
                                      float(stop) if stop is not None and pd.notna(stop) else None,
                                      float(target) if target is not None and pd.notna(target) else None)
    status, exit_px, exit_dt = _resolve_barrier_daily(window, fill, target_f, stop_f, int(direction))
    gross = (float(exit_px) / fill - 1.0) * int(direction)
    net = gross - cost_frac(str(horizon), paper_default=True)
    fd = window.iloc[0]["date"]
    return {
        "status": status,
        "exit_price": float(exit_px),
        "exit_date": exit_dt.date() if hasattr(exit_dt, "date") else exit_dt,
        "entry_fill_price": fill,
        "entry_fill_date": fd.date() if hasattr(fd, "date") else fd,
        "realized_ret": round(float(gross), 6),
        "realized_ret_net": round(float(net), 6),
    }


def resolve_open_ideas(asof: Optional[date] = None) -> dict:
    """Resolve every OPEN cash/future idea whose barrier window has elapsed."""
    asof = asof or date.today()
    open_df = read_sql(
        """SELECT idea_id, symbol, horizon, direction, entry, stop, target, created_at
             FROM trade_ideas
            WHERE status = 'OPEN'
              AND structure IN ('CASH_LONG', 'CASH_SHORT', 'FUTURE_LONG', 'FUTURE_SHORT')
         ORDER BY created_at"""
    )
    counts = {"checked": int(len(open_df)), "closed": 0, "win": 0,
              "loss": 0, "time": 0, "still_open": 0}
    if open_df.empty:
        return counts

    for _, r in open_df.iterrows():
        sessions = _HORIZON_SESSIONS.get(str(r["horizon"]).lower(), 5)
        entry_date = pd.to_datetime(r["created_at"]).date()
        bars = read_sql(
            """SELECT date, open, high, low, close FROM ohlcv
                WHERE symbol = :s AND date > :d ORDER BY date LIMIT :n""",
            params={"s": r["symbol"], "d": entry_date, "n": sessions + 6},
        )
        if not bars.empty:
            bars["date"] = pd.to_datetime(bars["date"])
        outcome = resolve_idea_outcome(r["entry"], r["stop"], r["target"],
                                       r["direction"], r["horizon"], bars)
        if outcome is None:
            counts["still_open"] += 1
            continue
        execute_sql(
            """UPDATE trade_ideas
                  SET status = %(st)s, exit_price = %(xp)s, exit_date = %(xd)s,
                      realized_ret = %(g)s, realized_ret_net = %(n)s, resolved_at = NOW(),
                      entry_fill_price = %(fp)s, entry_fill_date = %(fd)s
                WHERE idea_id = %(id)s""",
            {"st": outcome["status"], "xp": outcome["exit_price"], "xd": outcome["exit_date"],
             "g": outcome["realized_ret"], "n": outcome["realized_ret_net"],
             "fp": outcome["entry_fill_price"], "fd": outcome["entry_fill_date"],
             "id": r["idea_id"]},
        )
        counts["closed"] += 1
        counts[{"WIN": "win", "LOSS": "loss"}.get(outcome["status"], "time")] += 1

    log.info("Scorecard resolve: closed %d (win=%d loss=%d time=%d), %d still open",
             counts["closed"], counts["win"], counts["loss"], counts["time"],
             counts["still_open"])
    return counts


# ── Option-structure (iron-condor) resolution at expiry ───────────────────

def _default_settlement_lookup(underlying: str, expiry: date) -> Optional[float]:
    """Best-effort underlying settlement on `expiry` — the composed index-settlement
    source (persisted spot -> expiry-day bhavcopy UndrlygPric -> injected extras).
    Returns None when none can answer (idea stays OPEN — never guessed)."""
    from qsde.fno.settlement import index_settlement
    return index_settlement(underlying, expiry)


def resolve_option_ideas(asof: Optional[date] = None,
                         settlement_lookup: Optional[SettlementLookup] = None) -> dict:
    """Settle every OPEN IRON_CONDOR whose expiry has passed, scoring it against the
    underlying's settlement via the pure payoff. Mechanical (every expired idea, no
    hand-picking); skips any whose settlement isn't knowable yet."""
    asof = asof or date.today()
    look = settlement_lookup or _default_settlement_lookup
    open_df = read_sql(
        """SELECT idea_id, symbol, expiry, net_credit, max_loss_rupees, legs
             FROM trade_ideas
            WHERE status = 'OPEN' AND structure = 'IRON_CONDOR'
         ORDER BY created_at"""
    )
    counts = {"checked": int(len(open_df)), "closed": 0, "win": 0, "loss": 0,
              "still_open": 0, "no_settlement": 0}
    if open_df.empty:
        return counts

    for _, r in open_df.iterrows():
        expiry = r["expiry"]
        if expiry is None:
            counts["still_open"] += 1
            continue
        exp_date = expiry.date() if hasattr(expiry, "date") else expiry
        if exp_date > asof:
            counts["still_open"] += 1            # not expired yet
            continue
        settlement = look(r["symbol"], exp_date)
        if settlement is None:
            counts["no_settlement"] += 1         # expired but settlement unknown -> wait
            continue
        try:
            legs = r["legs"] if isinstance(r["legs"], list) else json.loads(r["legs"])
            p = condor_params_from_legs(legs)
            cost = estimate_condor_cost(float(r["net_credit"]))
            out = iron_condor_settlement_pnl(
                put_long=p["put_long"], put_short=p["put_short"],
                call_short=p["call_short"], call_long=p["call_long"],
                net_credit_rupees=float(r["net_credit"]), units=p["units"],
                settlement=float(settlement),
                max_loss_rupees=float(r["max_loss_rupees"]), cost_rupees=cost,
            )
        except Exception as e:  # noqa: BLE001 — one malformed idea must not stop the rest
            log.warning("resolve_option_ideas: skipping %s (%s)", r["idea_id"], e)
            continue
        execute_sql(
            """UPDATE trade_ideas
                  SET status = %(st)s, exit_price = %(xp)s, exit_date = %(xd)s,
                      realized_ret = %(g)s, realized_ret_net = %(n)s, resolved_at = NOW()
                WHERE idea_id = %(id)s""",
            {"st": out["status"], "xp": out["settlement"], "xd": exp_date,
             "g": out["realized_ret"], "n": out["realized_ret_net"], "id": r["idea_id"]},
        )
        counts["closed"] += 1
        counts["win" if out["status"] == "WIN" else "loss"] += 1

    log.info("Scorecard option-resolve: closed %d (win=%d loss=%d), %d still open, "
             "%d awaiting settlement", counts["closed"], counts["win"], counts["loss"],
             counts["still_open"], counts["no_settlement"])
    return counts


# ── Market-neutral PAIR (StatArb) resolution ──────────────────────────────

def resolve_pair_outcome(plan: dict, bars_a, bars_b) -> Optional[dict]:
    """Pure: replay a pair's spread forward against the FROZEN entry hedge/mean/sigma
    and score it. bars_a/bars_b: date/close ascending, AFTER entry. Exits WIN when
    |z| reverts to exit_z, LOSS when z runs to the adverse stop, TIME at max_hold.
    None if not enough forward data to have hit anything yet."""
    beta, alpha = float(plan["beta"]), float(plan["alpha"])
    mu, sigma = float(plan["spread_mean"]), float(plan["spread_sigma"])
    spread_0 = float(plan["spread_0"])
    exit_z, stop_z = float(plan["exit_z"]), float(plan["stop_z"])
    side, qty_a = int(plan["side"]), int(plan["qty_a"])
    max_loss = float(plan["max_loss"])
    max_hold = int(plan["max_hold"])
    if sigma <= 0:
        return None

    m = pd.merge(bars_a[["date", "close"]].rename(columns={"close": "a"}),
                 bars_b[["date", "close"]].rename(columns={"close": "b"}),
                 on="date", how="inner").sort_values("date")
    if m.empty:
        return None

    def _close(spread_t, z_t, status, exit_dt):
        pnl = qty_a * side * (spread_t - spread_0)
        pnl = max(-max_loss, pnl)                       # defined-risk floor
        from qsde.risk.costs import cost_frac
        cost = float(plan.get("gross_notional", 0.0)) * cost_frac("swing", paper_default=True)
        denom = max_loss if max_loss > 0 else 1.0
        return {"status": status, "exit_z": round(float(z_t), 4),
                "exit_date": exit_dt.date() if hasattr(exit_dt, "date") else exit_dt,
                "realized_ret": round(pnl / denom, 6),
                "realized_ret_net": round((pnl - cost) / denom, 6)}

    held = 0
    for _, row in m.iterrows():
        held += 1
        spread_t = float(row["a"]) - beta * float(row["b"]) - alpha
        z_t = (spread_t - mu) / sigma
        if abs(z_t) <= exit_z:                          # reverted -> WIN
            return _close(spread_t, z_t, "WIN", row["date"])
        if (side < 0 and z_t >= stop_z) or (side > 0 and z_t <= -stop_z):  # ran to stop -> LOSS
            return _close(spread_t, z_t, "LOSS", row["date"])
        if held >= max_hold:                            # time barrier
            return _close(spread_t, z_t, "TIME", row["date"])
    return None                                         # not enough forward sessions yet


def resolve_pair_ideas(asof: Optional[date] = None) -> dict:
    """Resolve every OPEN PAIR whose spread has hit an exit/stop/time barrier in the
    forward OHLCV. Mechanical; reuses the frozen entry plan in adjustment_plan."""
    asof = asof or date.today()
    open_df = read_sql(
        """SELECT idea_id, created_at, adjustment_plan
             FROM trade_ideas
            WHERE status = 'OPEN' AND structure = 'PAIR'
         ORDER BY created_at""")
    counts = {"checked": int(len(open_df)), "closed": 0, "win": 0, "loss": 0,
              "time": 0, "still_open": 0}
    if open_df.empty:
        return counts

    for _, r in open_df.iterrows():
        try:
            plan = r["adjustment_plan"]
            plan = plan if isinstance(plan, dict) else json.loads(plan)
            entry_date = pd.to_datetime(r["created_at"]).date()
            limit = int(plan.get("max_hold", 20)) + 6
            bars = {}
            for leg in ("sym_a", "sym_b"):
                b = read_sql(
                    "SELECT date, close FROM ohlcv WHERE symbol = :s AND date > :d "
                    "ORDER BY date LIMIT :n",
                    params={"s": plan[leg], "d": entry_date, "n": limit})
                if not b.empty:
                    b["date"] = pd.to_datetime(b["date"])
                bars[leg] = b
            outcome = resolve_pair_outcome(plan, bars["sym_a"], bars["sym_b"])
        except Exception as e:  # noqa: BLE001 — one malformed idea must not stop the rest
            log.warning("resolve_pair_ideas: skipping %s (%s)", r["idea_id"], e)
            continue
        if outcome is None:
            counts["still_open"] += 1
            continue
        execute_sql(
            """UPDATE trade_ideas
                  SET status = %(st)s, exit_price = %(xp)s, exit_date = %(xd)s,
                      realized_ret = %(g)s, realized_ret_net = %(n)s, resolved_at = NOW()
                WHERE idea_id = %(id)s""",
            {"st": outcome["status"], "xp": outcome["exit_z"], "xd": outcome["exit_date"],
             "g": outcome["realized_ret"], "n": outcome["realized_ret_net"], "id": r["idea_id"]},
        )
        counts["closed"] += 1
        counts[{"WIN": "win", "LOSS": "loss"}.get(outcome["status"], "time")] += 1

    log.info("Scorecard pair-resolve: closed %d (win=%d loss=%d time=%d), %d still open",
             counts["closed"], counts["win"], counts["loss"], counts["time"],
             counts["still_open"])
    return counts


__all__ = ["resolve_idea_outcome", "resolve_open_ideas", "resolve_option_ideas",
           "resolve_pair_outcome", "resolve_pair_ideas",
           "_default_settlement_lookup", "SettlementLookup", "_HORIZON_SESSIONS"]
