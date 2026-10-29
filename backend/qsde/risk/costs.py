"""
Horizon-aware transaction cost model.

Single source of truth for round-trip cost assumptions used by:
  * paper_journal.take_trade()         -> default cost_bps per horizon
  * paper_journal.reconcile_open_trades -> net return computation
  * edge_stats.json regeneration scripts
  * cap_governor                       -> Kelly fraction floor
  * dashboard "expected net" displays

Numbers below are Zerodha-realistic round-trip costs in basis points (bps),
where 1 bps = 0.01% of notional. They include brokerage, STT, exchange
transaction charges, GST, SEBI fee, stamp duty, and a modest slippage
allowance. They are intentionally CONSERVATIVE — better to under-promise.

INTRADAY (MIS):     ~6 bps round-trip
  brokerage     = 0.03% capped at Rs 20 -> ~3 bps on a typical retail ticket
  STT           = 0.025% on sell side    = 2.5 bps
  exchange + GST + SEBI + stamp          ~ 0.5 bps
  slippage      = 0.5 bps (large-cap, MIS, limit-order-aware)

BTST/SWING/LONG (CNC delivery):  ~24 bps round-trip (statutory floor)
  brokerage     = 0 on delivery (Zerodha)
  STT           = 0.1% on both sides     = 20 bps  <-- the dominant cost
  exchange + GST + SEBI + stamp + DP     ~ 4 bps
  --> statutory floor ~24 bps; add ~6 bps slippage on mid-caps => ~30 bps.
  CORRECTION (red-team 2026-07): STT is levied PER TRANSACTION on notional and
  does NOT amortize over the holding period. The earlier 12 bps figure (which
  hand-waved the 20 bps STT down via a bogus "multi-day amortization" argument)
  UNDERSTATED delivery cost by ~2x. Backtests must use >= 24 bps for CNC.

These numbers are tuned for the under-Rs-1-lakh bankroll case where the
Rs-20 brokerage cap binds. Larger bankrolls would see brokerage scale with
notional and these constants should rise accordingly.

DO NOT INLINE COSTS ELSEWHERE. Always import from here so a single
recalibration updates everywhere at once.
"""
from __future__ import annotations

from typing import Literal

Horizon = Literal["intraday", "swing", "long", "btst"]


# Round-trip cost in basis points (bps). 1 bps = 0.01% of notional.
# CNC delivery (btst/swing/long) recalibrated 12 -> 24 bps (red-team 2026-07):
# STT alone is 20 bps round-trip and does NOT amortize over the holding period.
#
# These tables are now DERIVED from the cost-model registry (qsde.ml.registry):
# the evaluation harness, the promotion gate and the Scorecard all charge the
# same numbers. Edit costs there, not here.
from qsde.ml.registry import round_trip_bps as _rt_bps

_NAMES = ("intraday", "btst", "swing", "long")
COSTS_BPS: dict[str, float] = {n: _rt_bps(n) for n in _NAMES}                   # 6 / 24 / 24 / 24

# Conservative paper-trade default (adds mid-cap slippage; avoids optimism bias
# on the live scorecard).
PAPER_DEFAULT_BPS: dict[str, float] = {n: _rt_bps(n, paper=True) for n in _NAMES}   # 8 / 30 / 30 / 30


def cost_bps(horizon: str, *, paper_default: bool = False) -> float:
    """Return round-trip cost in bps for a horizon.

    Args:
        horizon:       intraday / btst / swing / long.
        paper_default: if True, return the slightly conservative paper-trade
                       default (PAPER_DEFAULT_BPS); else the realistic cost.

    Returns:
        Round-trip cost in basis points. Falls back to 15 bps for unknown
        horizons (matches the legacy default in paper.py).
    """
    h = horizon.lower().strip()
    table = PAPER_DEFAULT_BPS if paper_default else COSTS_BPS
    return float(table.get(h, 15.0))


def cost_frac(horizon: str, *, paper_default: bool = False) -> float:
    """Same as cost_bps but returned as a fraction (e.g. 0.0006 for 6 bps)."""
    return cost_bps(horizon, paper_default=paper_default) / 10000.0


def all_costs() -> dict[str, dict[str, float]]:
    """Snapshot of the full cost table — for /api/risk/cap diagnostic output."""
    return {
        "realistic_bps":     dict(COSTS_BPS),
        "paper_default_bps": dict(PAPER_DEFAULT_BPS),
        "notes": (
            "Zerodha-realistic round-trip costs. Intraday MIS is dominated by "
            "STT 0.025% sell side + brokerage cap; delivery (BTST/Swing/Long) "
            "is dominated by STT 0.1% on both sides. Tuned for <Rs 1L bankroll."
        ),
    }
