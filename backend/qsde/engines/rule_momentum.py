"""
RuleMomentumEngine — first migration onto the Engine/TradeIdea contract
(Phase 0, step 5; ARCHITECTURE.md §6).

STRANGLER PATTERN: this wraps the already-persisted Tier-1 rule signals (the
rule_engine + rule_signal_writer path keeps serving in parallel) and re-emits
them as the ONE TradeIdea contract, sized via the ONE shared sizer
(qsde.engines.sizing.size_cash) so every emitted idea is DEFINED-RISK. Nothing
live is touched; the Engine simply EXISTS alongside, ready to be wired next.

The alpha is NOT re-derived — we adapt what the rule engine already produced.
`_to_idea` (signal-row -> TradeIdea) is pure and unit-tested without a DB.
"""
from __future__ import annotations

import logging
from typing import Callable, Optional

from qsde.engines.base import Engine, Horizon, Structure, TradeIdea
from qsde.engines.sizing import size_cash

log = logging.getLogger(__name__)

_HORIZON_MAP = {
    "intraday": Horizon.INTRADAY,
    "swing":    Horizon.SWING,
    "rotation": Horizon.ROTATION,
    "long":     Horizon.LONG,
}


# The ACTIONABLE tier-1 signal set — evidence-based, from the full-history,
# red-team-verified backtest (scripts/backtest_rules.py). Only tier1_mom (the
# jt+mop momentum composite) proved to be a durable net-of-cost edge that beats
# the benchmark, strongest on the SWING horizon (n=298 independent periods vs 74
# for long). BAB/RSI2 are beta/dead and the equal-weight composite dilutes, so
# they are written to `signals` for research/Scorecard comparison but are NOT
# turned into tradeable ideas here. Override via constructor to trade more.
DEFAULT_ACTIONABLE_STRATEGIES: tuple[str, ...] = ("tier1_mom",)
DEFAULT_ACTIONABLE_HORIZONS: tuple[str, ...] = ("swing",)


class RuleMomentumEngine(Engine):
    """Emits defined-risk cash TradeIdeas from the PROVEN liquid Tier-1 momentum signals."""

    name = "rule_momentum"
    horizon = Horizon.ROTATION
    clock_seconds = None   # EOD-driven

    def __init__(self, risk_rupees: float = 2000.0, budget: float = 1_000_000.0,
                 lot_size: int = 1,
                 source: Optional[Callable[[], list[dict]]] = None,
                 actionable_strategies: tuple[str, ...] = DEFAULT_ACTIONABLE_STRATEGIES,
                 actionable_horizons: tuple[str, ...] = DEFAULT_ACTIONABLE_HORIZONS,
                 long_only: bool = True) -> None:
        # Engines size-to-budget via the ONE shared sizer; the Risk Engine does
        # portfolio-level gating on top (ARCHITECTURE.md §sizing).
        self.risk_rupees = float(risk_rupees)
        self.budget = float(budget)
        self.lot_size = int(lot_size)
        # Which tier-1 strategies/horizons are ACTIONABLE (default: the proven
        # momentum composite on swing). Kept as attrs so _default_source filters
        # to exactly these; an injected `source` bypasses the filter entirely.
        self.actionable_strategies = tuple(actionable_strategies)
        self.actionable_horizons = tuple(actionable_horizons)
        # LONG-ONLY by default: the backtest validated only the long top-decile
        # (the +0.5 excess Sharpe); the short/bottom-decile leg is survivorship-
        # inflated (red-team: upper bound) AND not executable at retail (you can't
        # hold a short equity delivery position for a multi-day swing in India).
        self.long_only = bool(long_only)
        self._source = source or self._default_source

    def scan(self) -> list[TradeIdea]:
        ideas: list[TradeIdea] = []
        for sig in self._source():
            try:
                idea = self._to_idea(sig)
            except Exception as e:  # one bad row must not kill the scan
                log.warning("rule_momentum: skipping %s (%s)", sig.get("symbol"), e)
                idea = None
            if idea is not None:
                ideas.append(idea)
        return ideas

    def _to_idea(self, sig: dict) -> Optional[TradeIdea]:
        """Pure: one persisted rule-signal row -> a sized, defined-risk TradeIdea."""
        direction = int(sig.get("direction") or 0)
        if direction == 0:
            return None
        entry, stop = sig.get("entry_price"), sig.get("stop_price")
        if entry is None or stop is None:
            return None
        entry, stop = float(entry), float(stop)

        # ONE shared sizer — affordability + per-trade risk + lot rounding. No
        # engine reimplements sizing (that was the duplication the audit flagged).
        sized = size_cash(entry, entry, stop, budget=self.budget,
                          risk_per_trade=self.risk_rupees, lot_size=self.lot_size)
        if sized is None:
            return None   # untradeable in this budget / no usable risk

        target = sig.get("target_price")
        structure = Structure.CASH_LONG if direction > 0 else Structure.CASH_SHORT
        horizon = _HORIZON_MAP.get(str(sig.get("horizon", "")).lower(), self.horizon)
        strategy = sig.get("strategy", "tier1")

        idea = TradeIdea(
            engine=f"{self.name}:{strategy}",
            symbol=str(sig["symbol"]).upper(),
            structure=structure, horizon=horizon, direction=direction,
            entry=round(entry, 2), stop=round(stop, 2),
            target=round(float(target), 2) if target is not None else None,
            qty=sized["qty"], capital_required=sized["capital_required"],
            max_loss_rupees=sized["max_loss_rupees"],
            reasons=[f"{strategy} rule signal — qty {sized['qty']} "
                     f"(cap Rs{sized['capital_required']:.0f}, "
                     f"risk Rs{sized['max_loss_rupees']:.0f})"],
        )
        return idea.validate()

    def _default_source(self) -> list[dict]:
        """Latest liquid, ACTIONABLE Tier-1 signals from the signals table.

        Filtered to self.actionable_strategies x self.actionable_horizons (default
        tier1_mom / swing — the proven momentum edge). The other tier1_* rows stay
        in `signals` for the research dashboard + Scorecard IC comparison, but the
        engine does not emit them as tradeable ideas.
        """
        from qsde.db.connection import read_sql
        strat_list = ",".join(f"'{s}'" for s in self.actionable_strategies)
        hz_list = ",".join(f"'{h}'" for h in self.actionable_horizons)
        direction_clause = "direction > 0" if self.long_only else "direction <> 0"
        df = read_sql(
            f"""SELECT strategy, symbol, horizon, direction, entry_price,
                      target_price, stop_price, ranking_score, is_liquid
                 FROM signals
                WHERE strategy IN ({strat_list})
                  AND horizon IN ({hz_list})
                  AND {direction_clause}
                  AND COALESCE(is_liquid, FALSE) = TRUE
                  AND date = (SELECT MAX(date) FROM signals
                               WHERE strategy IN ({strat_list}))"""
        )
        return df.to_dict("records") if not df.empty else []


__all__ = ["RuleMomentumEngine"]
