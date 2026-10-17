"""Tests for the Scorecard outcome resolver (Phase 0 increment 2).

The pure barrier logic is exercised deterministically (no DB) via synthetic
forward bars, covering WIN / LOSS / TIME for long & short and the
not-enough-data case.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qsde.scorecard.resolver import resolve_idea_outcome


def _bars(rows) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=["date", "high", "low", "close"])
    df["date"] = pd.to_datetime(df["date"])
    return df


def test_long_win_hits_target_first():
    bars = _bars([("2026-06-03", 105, 99, 103), ("2026-06-04", 104, 100, 103),
                  ("2026-06-05", 106, 101, 105), ("2026-06-06", 107, 102, 106),
                  ("2026-06-07", 108, 103, 107)])
    out = resolve_idea_outcome(100, 98, 104, 1, "swing", bars)
    assert out["status"] == "WIN" and out["exit_price"] == 104
    assert out["realized_ret"] == pytest.approx(0.04)
    assert out["realized_ret_net"] < out["realized_ret"]   # cost subtracted


def test_long_loss_hits_stop_first():
    bars = _bars([("2026-06-03", 101, 97, 98), ("2026-06-04", 100, 96, 97),
                  ("2026-06-05", 99, 95, 96), ("2026-06-06", 98, 94, 95),
                  ("2026-06-07", 97, 93, 94)])
    out = resolve_idea_outcome(100, 98, 104, 1, "swing", bars)
    assert out["status"] == "LOSS" and out["exit_price"] == 98
    assert out["realized_ret"] < 0


def test_time_exit_when_neither_barrier_hit():
    bars = _bars([("2026-06-03", 101, 99, 100.0), ("2026-06-04", 101, 99, 100.5),
                  ("2026-06-05", 101, 99, 100.2), ("2026-06-06", 101, 99, 100.8),
                  ("2026-06-07", 101, 99, 101.0)])
    out = resolve_idea_outcome(100, 90, 110, 1, "swing", bars)
    assert out["status"] == "TIME" and out["exit_price"] == 101.0


def test_short_win_when_price_falls_to_target():
    bars = _bars([("2026-06-03", 101, 95, 96), ("2026-06-04", 100, 94, 95),
                  ("2026-06-05", 99, 93, 94), ("2026-06-06", 98, 92, 93),
                  ("2026-06-07", 97, 91, 92)])
    out = resolve_idea_outcome(100, 102, 96, -1, "swing", bars)
    assert out["status"] == "WIN" and out["exit_price"] == 96
    assert out["realized_ret"] > 0


def test_not_enough_forward_data_returns_none():
    bars = _bars([("2026-06-03", 105, 99, 103)])   # 1 bar < 5 sessions (swing)
    assert resolve_idea_outcome(100, 98, 104, 1, "swing", bars) is None
