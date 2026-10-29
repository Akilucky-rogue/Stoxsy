"""
Defined-risk option-structure builders (ARCHITECTURE.md §6, levels/).

Deterministic option-spread economics — the math the F&O-income engine uses to
turn chosen strikes + premiums into a validated, DEFINED-RISK TradeIdea. Every
structure here has a capped, finite max-loss BY CONSTRUCTION (the audit's
non-negotiable). Premiums are per-unit (per share / index point); rupee amounts
scale by lots * lot_size.
"""
from __future__ import annotations

from datetime import date

from qsde.engines.base import Horizon, Leg, Structure, TradeIdea


def build_credit_vertical(
    *, underlying: str, opt_type: str,
    short_strike: float, long_strike: float,
    short_premium: float, long_premium: float,
    lots: int, lot_size: int, expiry: date,
    short_symbol: str, long_symbol: str,
    engine: str = "fno_income",
) -> TradeIdea:
    """Defined-risk credit vertical: sell the near strike, buy the protective wing.

    opt_type 'PE' -> bull put spread (direction +1); 'CE' -> bear call spread (-1).
    max_loss = (strike width - net credit) * units — capped by the long wing.
    """
    opt_type = opt_type.upper()
    if opt_type not in ("CE", "PE"):
        raise ValueError(f"opt_type must be CE or PE, got {opt_type!r}")
    units = int(lots) * int(lot_size)
    if units <= 0:
        raise ValueError("lots * lot_size must be > 0")
    width = abs(float(short_strike) - float(long_strike))
    credit = float(short_premium) - float(long_premium)          # per unit
    if credit <= 0:
        raise ValueError("not a credit spread: short premium must exceed long premium")
    max_loss_unit = width - credit
    if max_loss_unit <= 0:
        raise ValueError("credit >= width implies arbitrage/bad inputs; refusing")

    direction = +1 if opt_type == "PE" else -1
    breakeven = (short_strike - credit) if opt_type == "PE" else (short_strike + credit)
    legs = [
        Leg(short_symbol, -1, units, strike=short_strike, expiry=expiry, opt_type=opt_type),
        Leg(long_symbol, +1, units, strike=long_strike, expiry=expiry, opt_type=opt_type),
    ]
    idea = TradeIdea(
        engine=engine, symbol=underlying, structure=Structure.VERTICAL_SPREAD,
        horizon=Horizon.EXPIRY, direction=direction, legs=legs,
        net_credit=round(credit * units, 2),
        max_profit=round(credit * units, 2),
        max_loss_rupees=round(max_loss_unit * units, 2),
        breakevens=[round(breakeven, 2)], expiry=expiry,
        reasons=[f"{'bull put' if opt_type == 'PE' else 'bear call'} credit spread "
                 f"{short_strike}/{long_strike}, credit {credit:.2f}/unit"],
    )
    return idea.validate()


def build_iron_condor(
    *, underlying: str,
    put_short: float, put_long: float, call_short: float, call_long: float,
    put_short_prem: float, put_long_prem: float,
    call_short_prem: float, call_long_prem: float,
    lots: int, lot_size: int, expiry: date,
    symbols: dict, engine: str = "fno_income",
) -> TradeIdea:
    """Defined-risk iron condor (neutral income): short put spread + short call spread.

    `symbols` maps {'ps','pl','cs','cl'} -> tradingsymbols.
    max_loss = (widest wing - total credit) * units; only one side can breach at expiry.
    """
    units = int(lots) * int(lot_size)
    if units <= 0:
        raise ValueError("lots * lot_size must be > 0")
    if not (put_long < put_short < call_short < call_long):
        raise ValueError("strikes must satisfy put_long < put_short < call_short < call_long")
    put_credit = float(put_short_prem) - float(put_long_prem)
    call_credit = float(call_short_prem) - float(call_long_prem)
    total_credit = put_credit + call_credit                      # per unit
    if total_credit <= 0:
        raise ValueError("not a credit condor: total credit must be > 0")
    put_width = put_short - put_long
    call_width = call_long - call_short
    max_loss_unit = max(put_width, call_width) - total_credit
    if max_loss_unit <= 0:
        raise ValueError("credit >= widest wing implies bad inputs; refusing")

    legs = [
        Leg(symbols["ps"], -1, units, strike=put_short, expiry=expiry, opt_type="PE"),
        Leg(symbols["pl"], +1, units, strike=put_long, expiry=expiry, opt_type="PE"),
        Leg(symbols["cs"], -1, units, strike=call_short, expiry=expiry, opt_type="CE"),
        Leg(symbols["cl"], +1, units, strike=call_long, expiry=expiry, opt_type="CE"),
    ]
    idea = TradeIdea(
        engine=engine, symbol=underlying, structure=Structure.IRON_CONDOR,
        horizon=Horizon.EXPIRY, direction=0, legs=legs,
        net_credit=round(total_credit * units, 2),
        max_profit=round(total_credit * units, 2),
        max_loss_rupees=round(max_loss_unit * units, 2),
        breakevens=[round(put_short - total_credit, 2), round(call_short + total_credit, 2)],
        expiry=expiry,
        reasons=[f"iron condor {put_long}/{put_short}-{call_short}/{call_long}, "
                 f"credit {total_credit:.2f}/unit"],
    )
    return idea.validate()


__all__ = ["build_credit_vertical", "build_iron_condor"]
