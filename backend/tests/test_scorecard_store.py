"""Tests for the Scorecard idea-log serializer (Phase 0 increment 1).

idea_to_row is pure (no DB): it must turn enums into string values and serialize
legs/greeks/dates into JSON — the serialization gap the self-review flagged.
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qsde.engines.base import Greeks, Horizon, Leg, Structure, TradeIdea
from qsde.scorecard.store import idea_to_row


def test_idea_to_row_cash_serializes_enums_and_reasons():
    idea = TradeIdea(
        engine="rule_momentum:tier1_jt", symbol="RELIANCE", structure=Structure.CASH_LONG,
        horizon=Horizon.SWING, direction=1, entry=100.0, stop=98.0, target=104.0,
        qty=500, capital_required=50000.0, max_loss_rupees=1000.0, reasons=["a", "b"],
    )
    row = idea_to_row(idea)
    assert row["structure"] == "CASH_LONG"          # enum -> value string (not "Structure.CASH_LONG")
    assert row["horizon"] == "swing"
    assert row["direction"] == 1 and row["qty"] == 500
    assert row["max_loss_rupees"] == 1000.0
    assert json.loads(row["reasons"]) == ["a", "b"]
    assert row["idea_id"] == idea.idea_id and row["scorecard_id"] == idea.scorecard_id


def test_idea_to_row_option_serializes_legs_dates_greeks():
    legs = [Leg("NIFTY24JUL25000CE", -1, 50, strike=25000, expiry=date(2026, 7, 31), opt_type="CE")]
    idea = TradeIdea(
        engine="fno_income", symbol="NIFTY", structure=Structure.IRON_CONDOR,
        horizon=Horizon.EXPIRY, direction=0, legs=legs, net_credit=4000.0, max_profit=4000.0,
        max_loss_rupees=6000.0, expiry=date(2026, 7, 31), greeks=Greeks(delta=0.1, theta=-5.0),
        breakevens=[23900.0, 24600.0],
    )
    row = idea_to_row(idea)
    assert row["structure"] == "IRON_CONDOR"
    legs_j = json.loads(row["legs"])
    assert legs_j[0]["opt_type"] == "CE"
    assert legs_j[0]["expiry"] == "2026-07-31"       # date -> ISO string inside JSON
    assert json.loads(row["greeks"]) == {"delta": 0.1, "gamma": None, "theta": -5.0, "vega": None}
    assert json.loads(row["breakevens"]) == [23900.0, 24600.0]
    assert row["expiry"] == date(2026, 7, 31)        # top-level DATE column keeps a date object


def test_idea_to_row_no_greeks_is_null():
    idea = TradeIdea(engine="e", symbol="X", structure=Structure.CASH_LONG,
                     horizon=Horizon.SWING, direction=1, entry=10, stop=9,
                     max_loss_rupees=100.0)
    assert idea_to_row(idea)["greeks"] is None


def test_idea_to_row_includes_idea_date_for_dedup():
    # idea_date is the YYYY-MM-DD of created_at — the per-day dedup key
    # (UNIQUE(engine, symbol, idea_date, structure)).
    idea = TradeIdea(engine="rule_momentum:tier1_jt", symbol="RELIANCE",
                     structure=Structure.CASH_LONG, horizon=Horizon.SWING, direction=1,
                     entry=100, stop=98, max_loss_rupees=1000.0,
                     created_at="2026-06-20T10:30:00")
    assert idea_to_row(idea)["idea_date"] == "2026-06-20"
