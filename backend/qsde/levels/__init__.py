"""levels/ — the ONE place that turns a decision into prices/structures.

CASH: re-exports the canonical compute_trade_levels (CORR-1-fixed, takes ATR as
PERCENT) from qsde.risk.trade_levels — NO duplication; the old scattered copies
are deleted as engines migrate. OPTIONS: defined-risk structure builders.
Every engine imports levels from here.
"""
from qsde.levels.options import build_credit_vertical, build_iron_condor
from qsde.risk.trade_levels import compute_trade_levels

__all__ = ["compute_trade_levels", "build_credit_vertical", "build_iron_condor"]
