"""
Shared position sizing for ALL engines — ONE sizer, zero duplication.

Wraps qsde.risk.budget_screener.size_position (the canonical, unit-tested sizer
that enforces BOTH constraints — affordability (budget) AND per-trade risk — plus
lot rounding) and packages the result for a cash/future TradeIdea.

Ownership rule (ARCHITECTURE.md §sizing): ENGINES size-to-budget here so every
emitted idea is defined-risk; the RISK ENGINE then gates at the PORTFOLIO level
(aggregate caps, vol-regime, dedup) — it does NOT re-size individual ideas. No
engine may reimplement sizing.
"""
from __future__ import annotations

from typing import Optional

from qsde.risk.budget_screener import size_position


def size_cash(price: float, entry: float, stop: float, *,
              budget: float, risk_per_trade: float, lot_size: int = 1) -> Optional[dict]:
    """Size a cash/future leg.

    Returns {qty, max_loss_rupees, capital_required, risk_at_stop} or None if the
    name isn't tradeable in this budget / has no usable stop.
    max_loss_rupees == risk_at_stop == qty * |entry - stop| (the defined risk).
    """
    s = size_position(price, entry, stop, budget=budget,
                      risk_per_trade=risk_per_trade, lot_size=lot_size)
    if not s.get("affordable") or int(s.get("final_qty", 0)) <= 0:
        return None
    risk = s.get("risk_at_stop")
    if risk is None or float(risk) <= 0:
        return None
    return {
        "qty": int(s["final_qty"]),
        "max_loss_rupees": round(float(risk), 2),
        "capital_required": round(float(s["capital_required"]), 2),
        "risk_at_stop": round(float(risk), 2),
    }


__all__ = ["size_cash"]
