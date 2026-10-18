"""
Paper-trade journal — the live-validation loop.

The backtest says ~0.9 intraday / ~0.43 long net Sharpe. This module lets you
find out if that holds on data the model has never seen, by:

  1. take_trade()          — record a signal you'd actually take (entry price,
                             target, stop, horizon), once per (symbol, horizon,
                             day, strategy).
  2. reconcile_open_trades — walk forward through real OHLCV (daily) /
                             ohlcv_intraday (minute) and mark each OPEN trade
                             against its triple barriers: WIN (target first),
                             LOSS (stop first), TIME (neither -> exit at horizon
                             close). Realized return is net of an assumed
                             round-trip cost so it's directly comparable to the
                             stress-tested net Sharpe.
  3. track_record()        — aggregate the closed trades into a live scorecard:
                             hit rate, avg net return, realized Sharpe — next to
                             the backtested edge band, so you can see whether
                             reality matches the backtest.

Strategy column (since migration 010):
  Every paper trade carries a `strategy` tag. The ML signals use "model";
  baseline strategies use "baseline_top_momentum", "baseline_nifty",
  "baseline_random". The drift/scorecard reports compare model vs baselines
  so we never deploy a model that's just adding overhead instead of edge.

Long-only by default (Indian retail can't easily short cash equity). The
barrier logic mirrors qsde/models/triple_barrier so labels and live outcomes
speak the same language.
"""
from __future__ import annotations

import logging
import random
from datetime import date
from typing import Optional

import numpy as np
import pandas as pd

from qsde.db.connection import execute_sql, read_sql
from qsde.risk.costs import cost_bps as _horizon_cost_bps

log = logging.getLogger(__name__)

# Horizon -> barrier window in NSE sessions. Mirrors triple_barrier t1.
from qsde.ml.registry import legacy_sessions

_HORIZON_SESSIONS = legacy_sessions()          # registry-derived


# ── take ────────────────────────────────────────────────────────────

def take_trade(symbol: str, horizon: str, cost_bps: Optional[float] = None,
               entry_price: Optional[float] = None,
               strategy: str = "model") -> dict:
    """Record a paper trade from the latest signal for (symbol, horizon).

    Pulls entry/target/stop/rank from the most recent signals row. Entry
    price defaults to the signal's entry_price (latest close); pass an
    explicit entry_price to use your actual decision-time price (more honest
    for intraday).

    Args:
        symbol:      NSE symbol.
        horizon:     intraday | swing | long.
        cost_bps:    Round-trip cost in bps. None -> horizon-aware default
                     from qsde.risk.costs (PAPER_DEFAULT_BPS).
        entry_price: Decision-time price; defaults to signal's entry_price.
        strategy:    Tag identifying which strategy generated this trade.
                     "model" for the ML signal (default). For baselines see
                     take_baseline_trades().
    """
    sym = symbol.upper().strip()
    hzn = horizon.lower().strip()
    if hzn not in _HORIZON_SESSIONS:
        return {"ok": False, "error": f"unknown horizon {hzn}"}
    if cost_bps is None:
        cost_bps = _horizon_cost_bps(hzn, paper_default=True)

    sig = read_sql(
        """SELECT symbol, horizon, direction, entry_price, target_price,
                  stop_price, ranking_score, is_liquid, date
             FROM signals
            WHERE symbol = :s AND horizon = :h
         ORDER BY date DESC LIMIT 1""",
        params={"s": sym, "h": hzn},
    )
    if sig.empty:
        return {"ok": False, "error": f"no signal for {sym}/{hzn}"}
    r = sig.iloc[0]
    if not bool(r["is_liquid"]):
        return {"ok": False, "error": f"{sym} is not liquid (ADV gate) — not tradeable"}
    direction = int(r["direction"]) or 1   # default long if HOLD slipped through
    # H8 fix: entry_price is NOT stamped at take-time. The EOD job runs after
    # 15:45 IST, so today's close is unattainable — you cannot buy today's
    # close after the close. Insert with entry_price NULL and let
    # fill_pending_entries() stamp it with the NEXT session's open on the
    # T+1 EOD run. `entry_price` parameter is retained for tests/tools that
    # want to override, but the default is next-open fill (honest).
    entry: Optional[float] = None
    if entry_price is not None:
        entry = float(entry_price)
        if entry <= 0:
            return {"ok": False, "error": "explicit entry_price must be > 0"}

    try:
        execute_sql(
            """INSERT INTO paper_trades
                 (symbol, horizon, entry_date, entry_price, direction,
                  target_price, stop_price, rank_pct, horizon_sessions,
                  cost_bps, strategy, entry_fill_date)
               VALUES
                 (%(s)s, %(h)s, %(d)s, %(e)s, %(dir)s, %(t)s, %(st)s, %(rk)s,
                  %(hs)s, %(c)s, %(strat)s, %(fd)s)
               ON CONFLICT (strategy, symbol, horizon, entry_date) DO NOTHING""",
            {
                "s": sym, "h": hzn, "d": date.today(), "e": entry, "dir": direction,
                "t": float(r["target_price"]) if pd.notna(r["target_price"]) else None,
                "st": float(r["stop_price"]) if pd.notna(r["stop_price"]) else None,
                "rk": float(r["ranking_score"]) if pd.notna(r["ranking_score"]) else None,
                "hs": _HORIZON_SESSIONS[hzn], "c": cost_bps,
                "strat": strategy,
                # If caller passed an explicit entry_price, treat that as
                # an already-attained fill; otherwise leave fill_date NULL.
                "fd": date.today() if entry is not None else None,
            },
        )
        return {"ok": True, "symbol": sym, "horizon": hzn,
                "entry": entry, "pending_fill": entry is None,
                "direction": direction, "strategy": strategy,
                "cost_bps": cost_bps}
    except Exception as e:  # noqa: BLE001
        log.exception("take_trade failed")
        return {"ok": False, "error": str(e)}


# ── baselines ───────────────────────────────────────────────────────

# Baseline strategies are the null we benchmark the ML model against. If
# the model can't beat ALL of these on net Sharpe over 30+ sessions,
# either the model is overhead or the cost model is wrong — either way,
# don't deploy real money on it.

_BASELINE_STRATEGIES = (
    "baseline_top_momentum",   # buy yesterday's top-3 NIFTY 200 movers
    "baseline_nifty",          # buy/hold NIFTY 200 representative
    "baseline_random",         # random pick from liquid universe
)

# Tier 1 rule-based factor strategies (added Phase D, migration 011).
# tier1_composite is the IC-weighted blend; the per-factor streams (jt, mop,
# bab, rsi2) are tracked separately so we can DSR each individually and see
# which factor is actually contributing edge over time.
_TIER1_STRATEGIES = (
    "tier1_jt",          # Jegadeesh-Titman 12-1 cross-sectional momentum
    "tier1_mop",         # Moskowitz-Ooi-Pedersen time-series momentum
    "tier1_bab",         # Frazzini-Pedersen betting-against-beta
    "tier1_rsi2",        # Connors-Alvarez RSI(2) mean reversion
    "tier1_composite",   # IC-weighted blend of the above
)


def _atr_levels(symbol: str, entry: float, horizon: str,
                direction: int = 1) -> tuple[Optional[float], Optional[float]]:
    """ATR-based target/stop for baseline trades — same multipliers as the
    model so realized stats are comparable like-for-like."""
    from qsde.features.store import latest_factor

    atr_s = latest_factor("tech_atr_pct", symbols=[symbol], max_age_days=45)
    atr_pct = float(atr_s.iloc[-1]) if len(atr_s) else None
    if atr_pct is None:
        # Per-horizon fallback ATR (matches qsde/risk/trade_levels._ATR_FALLBACK).
        atr_pct = {"intraday": 1.0, "swing": 2.2, "long": 4.5}.get(horizon, 2.2)
    # tech_atr_pct (and the fallback above) are PERCENT; convert to a fraction
    # UNCONDITIONALLY. Same CORR-1 fix as qsde/risk/trade_levels: the old
    # ">1.0 means percent" guess misread low-vol names (ATR% < 1.0) as a ~97%
    # fraction, blowing up the baseline trade's stop/target.
    atr_frac = atr_pct / 100.0
    # Multipliers mirror qsde/risk/trade_levels._HORIZON_MULTIPLIERS.
    s_mult, t_mult = {
        "intraday": (0.75, 1.50),
        "swing":    (1.50, 2.50),
        "long":     (2.50, 4.50),
    }.get(horizon, (1.50, 2.50))
    atr_abs = atr_frac * entry
    if direction > 0:
        return (entry + t_mult * atr_abs, entry - s_mult * atr_abs)
    return (entry - t_mult * atr_abs, entry + s_mult * atr_abs)


def _latest_close(symbol: str) -> Optional[float]:
    df = read_sql(
        "SELECT close FROM ohlcv WHERE symbol = :s ORDER BY date DESC LIMIT 1",
        params={"s": symbol},
    )
    return float(df.iloc[0]["close"]) if not df.empty else None


def _baseline_top_momentum_picks(n: int = 3) -> list[str]:
    """Yesterday's top-N NIFTY 200 daily-return names (long-only).

    Uses universe.index_membership (JSONB array; e.g. ["NIFTY 200","NIFTY 500"])
    rather than a separate membership table. Falls back to the active liquid
    universe if no rows match (e.g. on day 1 before index_membership is
    populated)."""
    df = read_sql(
        """WITH ranked AS (
              SELECT o.symbol, o.date,
                     (o.close / LAG(o.close) OVER (PARTITION BY o.symbol ORDER BY o.date) - 1) AS ret
                FROM ohlcv o
                JOIN universe u ON u.symbol = o.symbol
               WHERE u.is_active = TRUE
                 AND u.index_membership @> '["NIFTY 200"]'::jsonb
                 AND o.date >= CURRENT_DATE - INTERVAL '7 days'
           )
           SELECT symbol, ret FROM ranked
            WHERE date = (SELECT MAX(date) FROM ranked) AND ret IS NOT NULL
         ORDER BY ret DESC LIMIT :n""",
        params={"n": n},
    )
    if not df.empty:
        return [str(s) for s in df["symbol"].tolist()]
    # Fallback: if no NIFTY 200 membership rows yet, pull from liquid signals.
    df = read_sql(
        """WITH ranked AS (
              SELECT o.symbol, o.date,
                     (o.close / LAG(o.close) OVER (PARTITION BY o.symbol ORDER BY o.date) - 1) AS ret
                FROM ohlcv o
                JOIN universe u ON u.symbol = o.symbol
               WHERE u.is_active = TRUE
                 AND o.date >= CURRENT_DATE - INTERVAL '7 days'
           )
           SELECT symbol, ret FROM ranked
            WHERE date = (SELECT MAX(date) FROM ranked) AND ret IS NOT NULL
         ORDER BY ret DESC LIMIT :n""",
        params={"n": n},
    )
    return [str(s) for s in df["symbol"].tolist()] if not df.empty else []


def _baseline_nifty_proxy_picks() -> list[str]:
    """Use a fixed basket of 5 NIFTY 50 mega-caps as the index proxy. Buying
    a single ETF is cleaner but proxies a simple 'long Nifty' default for
    the paper journal."""
    return ["RELIANCE", "HDFCBANK", "ICICIBANK", "INFY", "TCS"]


def _baseline_random_picks(n: int = 3) -> list[str]:
    """Random picks from the currently-liquid universe (matches model's
    ADV gate so cost assumptions are comparable)."""
    df = read_sql(
        """SELECT DISTINCT symbol FROM signals
            WHERE is_liquid = TRUE
              AND date = (SELECT MAX(date) FROM signals)"""
    )
    pool = df["symbol"].tolist() if not df.empty else []
    if len(pool) <= n:
        return pool
    return random.sample(pool, n)


def take_baseline_trades(horizon: str = "swing") -> dict:
    """Record one paper trade per baseline strategy for today.

    Called by daily_eod.py after model signals are generated. Each baseline
    picks its names independently of the model so the comparison is honest.
    """
    hzn = horizon.lower().strip()
    if hzn not in _HORIZON_SESSIONS:
        return {"ok": False, "error": f"unknown horizon {hzn}"}

    bps = _horizon_cost_bps(hzn, paper_default=True)
    today = date.today()
    out: dict[str, dict] = {}

    plans = {
        "baseline_top_momentum": _baseline_top_momentum_picks(3),
        "baseline_nifty":        _baseline_nifty_proxy_picks(),
        "baseline_random":       _baseline_random_picks(3),
    }

    for strat, picks in plans.items():
        if not picks:
            out[strat] = {"taken": 0, "skipped": "no_picks"}
            continue
        taken = 0
        errors: list[str] = []
        for sym in picks:
            # H8 fix: use latest close as REFERENCE for computing target/stop
            # levels (ATR-anchored — the levels' distances are close-enough
            # invariants across the overnight gap). Actual entry_price is
            # NULL and gets stamped by fill_pending_entries() at T+1 open.
            ref = _latest_close(sym)
            if ref is None or ref <= 0:
                errors.append(f"{sym}:no_price")
                continue
            tgt, stp = _atr_levels(sym, ref, hzn, direction=1)
            try:
                execute_sql(
                    """INSERT INTO paper_trades
                         (symbol, horizon, entry_date, entry_price, direction,
                          target_price, stop_price, rank_pct, horizon_sessions,
                          cost_bps, strategy, entry_fill_date)
                       VALUES
                         (%(s)s, %(h)s, %(d)s, NULL, 1, %(t)s, %(st)s, NULL,
                          %(hs)s, %(c)s, %(strat)s, NULL)
                       ON CONFLICT (strategy, symbol, horizon, entry_date) DO NOTHING""",
                    {"s": sym, "h": hzn, "d": today,
                     "t": tgt, "st": stp, "hs": _HORIZON_SESSIONS[hzn],
                     "c": bps, "strat": strat},
                )
                taken += 1
            except Exception as e:  # noqa: BLE001
                errors.append(f"{sym}:{e}")
        out[strat] = {"taken": taken, "picks": picks,
                      "errors": errors if errors else None}

    return {"ok": True, "horizon": hzn, "as_of": today.isoformat(), "results": out}


# ── tier 1 rule-based ───────────────────────────────────────────────

def take_tier1_trades(horizon: str = "swing") -> dict:
    """Record paper trades from every Tier 1 rule signal for today.

    Tier 1 emits 5 strategy streams per horizon (jt, mop, bab, rsi2,
    composite). This pulls all of today's signals tagged tier1_* with
    direction != 0 and is_liquid = TRUE, and writes one paper_trade row
    per (strategy, symbol).

    Called by daily_eod.py AFTER compute_rule_signals.py has populated
    the signals table.

    Tier 1 intraday is intentionally empty — daily-bar factors don't
    produce intraday alpha — so calls with horizon='intraday' return
    early with `taken=0`.
    """
    hzn = horizon.lower().strip()
    if hzn not in _HORIZON_SESSIONS:
        return {"ok": False, "error": f"unknown horizon {hzn}"}

    bps = _horizon_cost_bps(hzn, paper_default=True)
    today = date.today()

    # strategy LIKE 'tier1_%' is more portable than ANY(:array) across
    # SQLAlchemy/psycopg2 array-binding flavors; the prefix naming convention
    # ('tier1_jt', 'tier1_composite', ...) gives us a clean filter.
    sigs = read_sql(
        """SELECT strategy, symbol, direction, entry_price, target_price,
                  stop_price, ranking_score, is_liquid
             FROM signals
            WHERE horizon = :h
              AND date    = :d
              AND strategy LIKE 'tier1\\_%' ESCAPE '\\'
              AND direction <> 0
              AND COALESCE(is_liquid, FALSE) = TRUE""",
        params={"h": hzn, "d": today},
    )

    out: dict[str, dict] = {strat: {"taken": 0, "errors": []}
                            for strat in _TIER1_STRATEGIES}

    if sigs.empty:
        return {"ok": True, "horizon": hzn, "as_of": today.isoformat(),
                "results": out, "note": "no_tier1_signals"}

    for _, r in sigs.iterrows():
        strategy = str(r["strategy"])
        sym = str(r["symbol"])
        direction = int(r["direction"])
        # H8 fix: don't require signal.entry_price to be present. The signal
        # writer's entry_price (session close) is used as the REFERENCE for
        # target/stop distance, which the signal row already carries. Actual
        # entry gets NULL — filled at next-session open by fill_pending_entries().
        if not pd.notna(r["entry_price"]) or float(r["entry_price"]) <= 0:
            out[strategy]["errors"].append(f"{sym}:no_reference_price_for_levels")
            continue

        try:
            execute_sql(
                """INSERT INTO paper_trades
                     (symbol, horizon, entry_date, entry_price, direction,
                      target_price, stop_price, rank_pct, horizon_sessions,
                      cost_bps, strategy, entry_fill_date)
                   VALUES
                     (%(s)s, %(h)s, %(d)s, NULL, %(dir)s, %(t)s, %(st)s, %(rk)s,
                      %(hs)s, %(c)s, %(strat)s, NULL)
                   ON CONFLICT (strategy, symbol, horizon, entry_date) DO NOTHING""",
                {
                    "s": sym, "h": hzn, "d": today, "dir": direction,
                    "t": (float(r["target_price"])
                          if pd.notna(r["target_price"]) else None),
                    "st": (float(r["stop_price"])
                           if pd.notna(r["stop_price"]) else None),
                    "rk": (float(r["ranking_score"])
                           if pd.notna(r["ranking_score"]) else None),
                    "hs": _HORIZON_SESSIONS[hzn], "c": bps,
                    "strat": strategy,
                },
            )
            out[strategy]["taken"] += 1
        except Exception as e:  # noqa: BLE001
            out[strategy]["errors"].append(f"{sym}:{e}")

    # Clean up empty error lists for friendlier logs.
    for strategy in out:
        if not out[strategy]["errors"]:
            out[strategy]["errors"] = None

    return {"ok": True, "horizon": hzn, "as_of": today.isoformat(),
            "results": out}


# ── reconcile ───────────────────────────────────────────────────────

def _resolve_barrier_daily(prices: pd.DataFrame, entry_price: float,
                           target: Optional[float], stop: Optional[float],
                           direction: int) -> tuple[str, Optional[float], Optional[pd.Timestamp]]:
    """Walk daily bars chronologically; return (status, exit_price, exit_date).

    Delegates to `qsde.ml.barrier.walk_barriers`, the ONE barrier implementation
    shared with the label builder and the evaluation harness (so a model is graded
    on exactly what paper trading resolves). Rules: LONG target hit if HIGH>=target,
    stop hit if LOW<=stop (SHORT mirrors); a same-bar tie is a STOP (pessimistic);
    when a bar OPENS through a barrier the exit fills at the open (needs an `open`
    column; without one the gap rule is skipped); no hit -> TIME exit at the last
    bar's close.
    """
    from qsde.ml.barrier import walk_barriers

    out = walk_barriers(prices, stop=stop, target=target, direction=direction)
    return out["status"], out["exit_price"], out["exit_date"]


def reconcile_open_trades(asof: Optional[date] = None) -> dict:
    """Resolve every OPEN paper trade whose barrier window has elapsed.

    Returns counts {checked, closed, win, loss, time, still_open}.
    """
    asof = asof or date.today()
    # H8 fix: skip trades still waiting for next-open fill. Their entry_price
    # is NULL until fill_pending_entries() runs (should run BEFORE reconcile
    # in the EOD orchestrator).
    open_df = read_sql(
        """SELECT * FROM paper_trades
            WHERE status = 'OPEN'
              AND entry_price IS NOT NULL
              AND entry_fill_date IS NOT NULL
         ORDER BY entry_date"""
    )
    if open_df.empty:
        return {"checked": 0, "closed": 0, "win": 0, "loss": 0, "time": 0, "still_open": 0}

    n_win = n_loss = n_time = n_open = 0
    for _, t in open_df.iterrows():
        sym = t["symbol"]
        entry_date = pd.to_datetime(t["entry_date"]).date()
        # Pull daily bars AFTER entry up to the horizon window (+slack for holidays).
        window = int(t["horizon_sessions"]) + 6
        bars = read_sql(
            """SELECT date, open, high, low, close FROM ohlcv
                WHERE symbol = :s AND date > :d
             ORDER BY date LIMIT :n""",
            params={"s": sym, "d": entry_date, "n": window},
        )
        # Not enough forward data yet -> leave OPEN.
        if bars.empty or len(bars) < int(t["horizon_sessions"]):
            n_open += 1
            continue
        bars["date"] = pd.to_datetime(bars["date"])
        bars = bars.head(int(t["horizon_sessions"]))   # only the barrier window

        status, exit_px, exit_dt = _resolve_barrier_daily(
            bars, float(t["entry_price"]),
            float(t["target_price"]) if pd.notna(t["target_price"]) else None,
            float(t["stop_price"]) if pd.notna(t["stop_price"]) else None,
            int(t["direction"]),
        )
        gross = (exit_px / float(t["entry_price"]) - 1.0) * int(t["direction"])
        net = gross - float(t["cost_bps"]) / 10000.0

        execute_sql(
            """UPDATE paper_trades
                  SET status = %(st)s, exit_date = %(xd)s, exit_price = %(xp)s,
                      realized_ret = %(g)s, realized_ret_net = %(n)s
                WHERE id = %(id)s""",
            {"st": status, "xd": exit_dt.date() if hasattr(exit_dt, "date") else exit_dt,
             "xp": float(exit_px), "g": float(gross), "n": float(net), "id": int(t["id"])},
        )
        if status == "WIN":   n_win += 1
        elif status == "LOSS": n_loss += 1
        else:                  n_time += 1

    closed = n_win + n_loss + n_time
    log.info("Paper reconcile: closed %d (win=%d loss=%d time=%d), %d still open",
             closed, n_win, n_loss, n_time, n_open)
    return {"checked": len(open_df), "closed": closed, "win": n_win,
            "loss": n_loss, "time": n_time, "still_open": n_open}


# ── track record ────────────────────────────────────────────────────


# ── fill pending entries (H8) ───────────────────────────────────────

def _next_session_open(symbol: str, after_date: date) -> Optional[tuple[float, date]]:
    """Return (open_price, session_date) for the FIRST OHLCV bar strictly
    after `after_date` for `symbol`. Returns None if no such bar exists yet
    (e.g. the pending trade was booked but next-day data hasn't arrived).

    Uses `open` — the price you could actually transact at, which is the
    entire point of the H8 fix (signal-day close was unattainable).
    """
    df = read_sql(
        """SELECT open, date FROM ohlcv
            WHERE symbol = :s AND date > :d
              AND open IS NOT NULL
         ORDER BY date ASC LIMIT 1""",
        params={"s": symbol, "d": after_date},
    )
    if df.empty:
        return None
    return float(df.iloc[0]["open"]), pd.to_datetime(df.iloc[0]["date"]).date()


def fill_pending_entries(asof: Optional[date] = None) -> dict:
    """Fill every paper_trade whose entry_fill_date IS NULL, using the
    open of the FIRST OHLCV session strictly after entry_date (H8 fix).

    Called by daily_eod.py BEFORE reconcile_open_trades so the same-day
    reconciliation cycle picks up newly-filled trades.

    A trade that stays pending across the T+1 EOD run (because next-day
    OHLCV hasn't arrived yet — holiday, ingest lag) is safe: it just
    remains pending until a subsequent run. reconcile_open_trades skips
    NULL-entry rows.

    Returns:
        {checked, filled, still_pending, errors}
    """
    _ = asof or date.today()
    pending = read_sql(
        """SELECT id, symbol, entry_date
             FROM paper_trades
            WHERE entry_price IS NULL
              AND entry_fill_date IS NULL
              AND status = 'OPEN'
         ORDER BY entry_date"""
    )
    if pending.empty:
        return {"checked": 0, "filled": 0, "still_pending": 0, "errors": []}

    n_filled = 0
    n_pending = 0
    errors: list[str] = []
    for _, t in pending.iterrows():
        sym = str(t["symbol"])
        entry_dt = pd.to_datetime(t["entry_date"]).date()
        try:
            hit = _next_session_open(sym, entry_dt)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{sym}@{entry_dt}:lookup_failed:{e}")
            n_pending += 1
            continue
        if hit is None:
            n_pending += 1
            continue
        open_px, fill_dt = hit
        try:
            execute_sql(
                """UPDATE paper_trades
                      SET entry_price = %(px)s, entry_fill_date = %(fd)s
                    WHERE id = %(id)s""",
                {"px": float(open_px), "fd": fill_dt, "id": int(t["id"])},
            )
            n_filled += 1
        except Exception as e:  # noqa: BLE001
            errors.append(f"{sym}@{entry_dt}:update_failed:{e}")
            n_pending += 1

    log.info("fill_pending_entries: filled %d, still pending %d, %d errors",
             n_filled, n_pending, len(errors))
    return {"checked": len(pending), "filled": n_filled,
            "still_pending": n_pending, "errors": errors}


# ── track record ────────────────────────────────────────────────────
# NOTE: full track_record() implementation lives in older git history;
# this stub is the safe minimum so imports don't break during Stage 2B.1b.
# Full function will be restored in a subsequent session — its logic is
# preserved (per-strategy per-horizon block computed from realized_ret_net).

def track_record(horizon: Optional[str] = None,
                 strategy: Optional[str] = None) -> dict:
    """Live scorecard from closed paper trades. Stub — full implementation
    to be restored from git history in a subsequent session.
    """
    where = "WHERE status IN ('WIN','LOSS','TIME') AND entry_price IS NOT NULL"
    params: dict[str, object] = {}
    if horizon:
        where += " AND horizon = :h"
        params["h"] = horizon.lower()
    if strategy:
        where += " AND strategy = :strat"
        params["strat"] = strategy
    df = read_sql(f"SELECT * FROM paper_trades {where}", params=params)
    if df.empty:
        return {"n": 0, "note": "no closed paper trades yet"}
    rets = df["realized_ret_net"].astype(float)
    return {
        "n": int(len(df)),
        "wins": int((df["status"] == "WIN").sum()),
        "losses": int((df["status"] == "LOSS").sum()),
        "time_exits": int((df["status"] == "TIME").sum()),
        "win_rate": round(float((rets > 0).mean()), 3),
        "avg_net_ret_bps": round(float(rets.mean()) * 1e4, 1),
        "std_net_ret_bps": round(float(rets.std()) * 1e4, 1) if len(df) > 1 else None,
    }
