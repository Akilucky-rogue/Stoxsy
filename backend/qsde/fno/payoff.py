"""
Option-structure settlement P&L (F&O increment 5) — how an iron condor actually
resolves at expiry, so the Scorecard can measure the flagship's TRUE edge.

PURE + DB-free. The short iron condor keeps the full credit when the underlying
settles between the short strikes, and loses (capped at max_loss) as it breaches
a wing. P&L per unit at settlement S:

    pnl_unit = credit_unit
             - [max(put_short - S, 0) - max(put_long - S, 0)]      # put-spread liability
             - [max(S - call_short, 0) - max(S - call_long, 0)]    # call-spread liability

bounded in [-max_loss_unit, +credit_unit] by construction. Rupee P&L scales by
`units` (= lots * lot_size). Returns are risk-normalised (pnl / max_loss), so a
full win is +credit/max_loss and a full loss is -1.0 — comparable across condors.

Costs: index-option round-trip friction (brokerage + STT-on-exercise + exchange +
GST) is hard to know precisely pre-fill, so we subtract a TRANSPARENT estimate —
a fraction of the credit (QSDE_FNO_COST_FRAC, default 10%) — and label it an
estimate. Replace with realised fills once live.
"""
from __future__ import annotations

import os

DEFAULT_COST_FRAC = float(os.getenv("QSDE_FNO_COST_FRAC", "0.10"))


def condor_params_from_legs(legs: list[dict]) -> dict:
    """Recover {put_long, put_short, call_short, call_long, units} from a stored
    condor's legs (each: side ±1, strike, opt_type, qty). Raises if the four
    defined-risk legs aren't all present."""
    by = {}
    units = None
    for leg in legs:
        ot = str(leg.get("opt_type", "")).upper()
        side = int(leg.get("side", 0))
        strike = leg.get("strike")
        if ot not in ("CE", "PE") or strike is None:
            continue
        units = int(leg.get("qty") or units or 0)
        if ot == "PE" and side < 0:
            by["put_short"] = float(strike)
        elif ot == "PE" and side > 0:
            by["put_long"] = float(strike)
        elif ot == "CE" and side < 0:
            by["call_short"] = float(strike)
        elif ot == "CE" and side > 0:
            by["call_long"] = float(strike)
    missing = {"put_long", "put_short", "call_short", "call_long"} - by.keys()
    if missing or not units:
        raise ValueError(f"incomplete condor legs (missing {missing or 'units'})")
    by["units"] = int(units)
    return by


def iron_condor_settlement_pnl(
    *, put_long: float, put_short: float, call_short: float, call_long: float,
    net_credit_rupees: float, units: int, settlement: float,
    max_loss_rupees: float | None = None, cost_rupees: float = 0.0,
) -> dict:
    """Short-iron-condor P&L at expiry settlement `S`. Credit/loss in RUPEES.

    Returns {settlement, pnl_gross, pnl_net, status (WIN/LOSS), realized_ret,
    realized_ret_net} where returns are pnl / max_loss (risk-normalised)."""
    units = int(units)
    if units <= 0:
        raise ValueError("units must be > 0")
    S = float(settlement)
    credit_unit = float(net_credit_rupees) / units
    put_liab = max(put_short - S, 0.0) - max(put_long - S, 0.0)
    call_liab = max(S - call_short, 0.0) - max(S - call_long, 0.0)
    pnl = (credit_unit - put_liab - call_liab) * units
    # Clamp into the defined-risk envelope [-max_loss, +credit] (guards float fuzz).
    if max_loss_rupees is not None:
        pnl = max(-float(max_loss_rupees), min(float(net_credit_rupees), pnl))
    pnl_net = pnl - float(cost_rupees)

    denom = float(max_loss_rupees) if max_loss_rupees else abs(float(net_credit_rupees)) or 1.0
    return {
        "settlement": S,
        "pnl_gross": round(pnl, 2),
        "pnl_net": round(pnl_net, 2),
        "status": "WIN" if pnl_net > 0 else "LOSS",
        "realized_ret": round(pnl / denom, 6),
        "realized_ret_net": round(pnl_net / denom, 6),
    }


def estimate_condor_cost(net_credit_rupees: float, cost_frac: float = DEFAULT_COST_FRAC) -> float:
    """Transparent friction estimate = cost_frac * |credit| (default 10%). A
    placeholder until realised fills exist — NOT a precise brokerage model."""
    return abs(float(net_credit_rupees)) * float(cost_frac)


__all__ = ["condor_params_from_legs", "iron_condor_settlement_pnl",
           "estimate_condor_cost", "DEFAULT_COST_FRAC"]
