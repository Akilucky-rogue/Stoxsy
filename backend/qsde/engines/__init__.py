"""Strategy engines — each emits the one TradeIdea contract (ARCHITECTURE.md §5).

Phase 0: only the contract + base class exist. Concrete engines (fno_income,
momentum_rotation, factor_quality, intraday_assist) are migrated onto this one
at a time (strangler pattern) so the live flow never breaks.
"""
from qsde.engines.base import Engine, Greeks, Horizon, Leg, Structure, TradeIdea

__all__ = ["Structure", "Horizon", "Leg", "Greeks", "TradeIdea", "Engine"]
