"""
FnoIncomeEngine — the flagship: defined-risk INDEX option income (ARCHITECTURE.md §F&O).

Sells premium ONLY when implied vol is rich (iv_rank gate), as a DEFINED-RISK iron
condor (short ~Nσ-OTM strangle + protective wings) on European, cash-settled INDEX
options (NIFTY / BANKNIFTY) — so there is no assignment/early-exercise risk. Every
emitted idea is defined-risk by construction (build_iron_condor caps max_loss; the
engine also drops anything above its per-trade loss cap).

The chain SOURCE (Kite live / NSE Bhavcopy history) is INJECTED, so this strike-
selection brain is pure and fully unit-tested; data plumbing is a separate increment.

Each source snapshot:
    {underlying, spot, t_years, atm_iv, iv_rank (0..1), expiry: date,
     chain: {(strike, 'CE'|'PE'): premium}}
"""
from __future__ import annotations

import logging
from datetime import date
from math import sqrt
from typing import Callable, Optional

from qsde.engines.base import Engine, Horizon, TradeIdea
from qsde.levels.options import build_iron_condor

log = logging.getLogger(__name__)


def expected_move(spot: float, iv: float, t_years: float) -> float:
    """1-sigma move over t_years: spot * iv * sqrt(T)."""
    return spot * iv * sqrt(max(t_years, 0.0))


def _round_to_step(x: float, step: float) -> float:
    return round(x / step) * step


def _opt_symbol(underlying: str, expiry: date, strike: float, opt: str) -> str:
    # Readable idea-log symbol; execution maps to Kite's exact instrument later.
    return f"{underlying}{expiry:%d%b%y}{int(strike)}{opt}".upper()


class FnoIncomeEngine(Engine):
    name = "fno_income"
    horizon = Horizon.EXPIRY

    def __init__(self, *, lot_size: int, strike_step: float, iv_rank_min: float = 0.5,
                 otm_sd: float = 1.0, wing_points: float = 200.0, lots: int = 1,
                 max_loss_cap: float = float("inf"), min_iv_history: int = 0,
                 source: Optional[Callable[[], list[dict]]] = None) -> None:
        self.lot_size = int(lot_size)
        self.strike_step = float(strike_step)
        self.iv_rank_min = float(iv_rank_min)
        self.otm_sd = float(otm_sd)
        self.wing_points = float(wing_points)
        self.lots = int(lots)
        self.max_loss_cap = float(max_loss_cap)
        # Warm-up floor: require this many trailing ATM-IV sessions before a rank
        # is trusted. iv_rank itself already returns NaN for empty/degenerate
        # windows (suppressed below); this additionally refuses to sell on a thin
        # window even when it happens to be non-degenerate. Production sets ~20.
        self.min_iv_history = int(min_iv_history)
        self._source = source or (lambda: [])

    def scan(self) -> list[TradeIdea]:
        ideas: list[TradeIdea] = []
        for snap in self._source():
            try:
                idea = self._to_idea(snap)
            except Exception as e:  # one bad snapshot must not kill the scan
                log.warning("fno_income: skipping %s (%s)", snap.get("underlying"), e)
                idea = None
            if idea is not None:
                ideas.append(idea)
        return ideas

    def _to_idea(self, snap: dict) -> Optional[TradeIdea]:
        """Pure: one chain snapshot -> a defined-risk iron-condor TradeIdea, or None."""
        # Vol-rich gate. iv_rank is NaN on a cold/degenerate window — and since
        # `NaN < x` is False, a bare `<` comparison would WRONGLY let it pass and
        # emit a condor on no signal. So bail explicitly on a missing/NaN rank
        # BEFORE the threshold test, and refuse a too-thin warm-up window.
        iv_rank = snap.get("iv_rank")
        if iv_rank is None or iv_rank != iv_rank:          # missing / NaN -> no signal
            return None
        iv_rank = float(iv_rank)
        if iv_rank < self.iv_rank_min:
            return None   # vol not rich enough to sell premium
        if int(snap.get("iv_history_n", 0)) < self.min_iv_history:
            return None   # not enough trailing sessions to trust the rank yet

        spot = float(snap["spot"])
        t_years = float(snap["t_years"])
        atm_iv = float(snap["atm_iv"])
        expiry = snap["expiry"]
        chain = snap["chain"]
        underlying = str(snap["underlying"])

        # Per-index structure (NIFTY vs BANKNIFTY differ) can ride on the
        # snapshot so ONE engine instance handles every index; falls back to the
        # engine's own defaults when the snapshot doesn't specify.
        lot_size = int(snap.get("lot_size") or self.lot_size)
        strike_step = float(snap.get("strike_step") or self.strike_step)
        wing_points = float(snap.get("wing_points") or self.wing_points)

        move = expected_move(spot, atm_iv, t_years)
        if move <= 0:
            return None
        put_short = _round_to_step(spot - self.otm_sd * move, strike_step)
        call_short = _round_to_step(spot + self.otm_sd * move, strike_step)
        put_long = put_short - wing_points
        call_long = call_short + wing_points
        if not (put_long < put_short < call_short < call_long):
            return None

        try:
            ps_p = float(chain[(put_short, "PE")])
            pl_p = float(chain[(put_long, "PE")])
            cs_p = float(chain[(call_short, "CE")])
            cl_p = float(chain[(call_long, "CE")])
        except KeyError:
            return None   # a required strike isn't quoted in this chain

        idea = build_iron_condor(
            underlying=underlying,
            put_short=put_short, put_long=put_long, call_short=call_short, call_long=call_long,
            put_short_prem=ps_p, put_long_prem=pl_p, call_short_prem=cs_p, call_long_prem=cl_p,
            lots=self.lots, lot_size=lot_size, expiry=expiry,
            symbols={
                "ps": _opt_symbol(underlying, expiry, put_short, "PE"),
                "pl": _opt_symbol(underlying, expiry, put_long, "PE"),
                "cs": _opt_symbol(underlying, expiry, call_short, "CE"),
                "cl": _opt_symbol(underlying, expiry, call_long, "CE"),
            },
        )
        if idea.max_loss_rupees > self.max_loss_cap:
            return None   # too much risk for this book

        idea.confidence = round(iv_rank, 3)   # richer vol -> higher conviction
        idea.reasons.append(
            f"IV-rank {iv_rank:.2f} >= {self.iv_rank_min}; short ~{self.otm_sd:g}sigma "
            f"({int(put_short)}P/{int(call_short)}C), wings {int(wing_points)}"
        )
        # Stamp the idea with the SESSION date (not wall-clock) so the idea-log's
        # per-day dedup key (engine, symbol, idea_date, structure) is the session,
        # not the day we happened to run it — keeps backfills/re-runs idempotent
        # and attributes the idea to the right session in the Scorecard.
        asof = snap.get("asof")
        if asof is not None:
            idea.created_at = f"{asof.isoformat()}T18:00:00"
        return idea.validate()


__all__ = ["FnoIncomeEngine", "expected_move"]
