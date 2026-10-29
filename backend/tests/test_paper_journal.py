"""Tests for the paper-trade journal barrier-resolution logic (hermetic)."""
from __future__ import annotations

import pandas as pd
import pytest

from qsde.execution.paper_journal import _resolve_barrier_daily


def _bars(rows):
    return pd.DataFrame(rows, columns=["date", "high", "low", "close"]).assign(
        date=lambda d: pd.to_datetime(d["date"])
    )


class TestBarrierResolution:
    def test_long_hits_target(self):
        bars = _bars([
            ("2026-01-02", 102, 99, 101),
            ("2026-01-05", 106, 100, 105),   # high 106 >= target 105
        ])
        status, px, _ = _resolve_barrier_daily(bars, entry_price=100, target=105, stop=95, direction=1)
        assert status == "WIN" and px == 105

    def test_long_hits_stop(self):
        bars = _bars([
            ("2026-01-02", 101, 94, 96),     # low 94 <= stop 95
        ])
        status, px, _ = _resolve_barrier_daily(bars, entry_price=100, target=105, stop=95, direction=1)
        assert status == "LOSS" and px == 95

    def test_same_bar_tie_is_pessimistic_loss(self):
        # A bar that touches BOTH target and stop -> we assume STOP first.
        bars = _bars([
            ("2026-01-02", 106, 94, 100),    # high>=105 AND low<=95
        ])
        status, px, _ = _resolve_barrier_daily(bars, entry_price=100, target=105, stop=95, direction=1)
        assert status == "LOSS" and px == 95

    def test_time_exit_when_neither_hit(self):
        bars = _bars([
            ("2026-01-02", 103, 98, 102),
            ("2026-01-05", 104, 99, 103),    # neither 105 nor 95 touched
        ])
        status, px, _ = _resolve_barrier_daily(bars, entry_price=100, target=105, stop=95, direction=1)
        assert status == "TIME" and px == 103

    def test_chronological_order_first_barrier_wins(self):
        # Stop touched day 1, target touched day 2 -> LOSS (stop first).
        bars = _bars([
            ("2026-01-02", 101, 94, 96),     # stop first
            ("2026-01-05", 110, 100, 108),   # target later, ignored
        ])
        status, px, _ = _resolve_barrier_daily(bars, entry_price=100, target=105, stop=95, direction=1)
        assert status == "LOSS" and px == 95


class TestFillConvention:
    def test_levels_keep_their_fractional_offsets_across_the_overnight_gap(self):
        from qsde.ml.barrier import levels_at_fill
        stop, tgt = levels_at_fill(100.0, 110.0, 98.0, 104.0)
        assert stop == pytest.approx(107.8) and tgt == pytest.approx(114.4)

    def test_short_levels_scale_the_same_way(self):
        from qsde.ml.barrier import levels_at_fill
        stop, tgt = levels_at_fill(100.0, 90.0, 102.0, 96.0)          # stop above, target below
        assert stop == pytest.approx(91.8) and tgt == pytest.approx(86.4)

    def test_no_reference_means_levels_are_untouched(self):
        from qsde.ml.barrier import levels_at_fill
        assert levels_at_fill(None, 110.0, 98.0, 104.0) == (98.0, 104.0)
        assert levels_at_fill(100.0, 110.0, None, None) == (None, None)
