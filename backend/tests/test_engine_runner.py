"""Tests for the engine runner (Phase 0 increment 1) — scan + record, no DB."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qsde.engines.rule_momentum import RuleMomentumEngine
from qsde.engines.runner import run_engine


def test_run_engine_scans_and_records_every_idea():
    rows = [
        {"strategy": "tier1_jt", "symbol": "RELIANCE", "horizon": "rotation", "direction": 1,
         "entry_price": 100, "stop_price": 98, "target_price": 105, "is_liquid": True},
        {"strategy": "tier1_mop", "symbol": "TCS", "horizon": "long", "direction": -1,
         "entry_price": 200, "stop_price": 204, "target_price": 190, "is_liquid": True},
    ]
    eng = RuleMomentumEngine(budget=1_000_000, source=lambda: rows)
    recorded: list = []
    ideas = run_engine(eng, recorder=lambda batch: recorded.extend(batch) or len(list(recorded)))
    assert len(ideas) == 2
    # The anti-gaming guarantee: EVERY emitted idea was recorded (not a subset).
    assert len(recorded) == 2
    assert {i.idea_id for i in ideas} == {i.idea_id for i in recorded}


def test_run_engine_records_nothing_when_no_ideas():
    eng = RuleMomentumEngine(source=lambda: [])
    recorded: list = []
    ideas = run_engine(eng, recorder=lambda batch: recorded.extend(batch))
    assert ideas == [] and recorded == []
