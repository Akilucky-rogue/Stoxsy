"""Regression tests for Stage 2C.1 of Session 2 (AUDIT_v2 H2 + H4).

H2 — Horizon windows in NSE SESSIONS, not calendar days.
    Prior: _HORIZON_BARRIERS["swing"]["t1_days"] = 5 → pd.Timedelta(days=5)
           lands ~3 sessions later after weekends. Labels and paper
           reconciliation measured different windows.
    Fix:   _HORIZON_BARRIERS["swing"]["t1_sessions"] = 5, and
           get_t1_barriers steps by POSITION in the price index.

H4 — Label barriers detect on high/low with pessimistic same-bar tie.
    Prior: _apply_pt_sl_on_t1 compared only `close` — labels systematically
           hit barriers less often than paper_journal (which uses high/low).
    Fix:   long: PT if high >= entry*(1+pt*σ), SL if low <= entry*(1-sl*σ);
           short: mirror; SL wins same-bar ties.

Hermetic — synthetic OHLC panels, no DB, no network.
"""
from __future__ import annotations

import inspect

import numpy as np
import pandas as pd
import pytest


# ─────────────────────────────────────────────────────────────────────
# H2 — session-based t1
# ─────────────────────────────────────────────────────────────────────

class TestH2SessionBasedT1:
    def test_horizon_barriers_use_t1_sessions_key(self):
        from qsde.models.triple_barrier import _HORIZON_BARRIERS
        for h in ("intraday", "swing", "long"):
            assert "t1_sessions" in _HORIZON_BARRIERS[h], (
                f"{h}: _HORIZON_BARRIERS must use 't1_sessions' key "
                f"(H2 fix), not 't1_days'."
            )
            # And the old key MUST be gone so nothing accidentally reads
            # 5 calendar days when we mean 5 sessions.
            assert "t1_days" not in _HORIZON_BARRIERS[h], (
                f"{h}: legacy 't1_days' key must be removed (H2 fix)."
            )

    def test_horizon_values_match_execution_windows(self):
        """The t1 windows must match what paper_journal._HORIZON_SESSIONS
        uses, or labels and reconciliation will diverge again."""
        from qsde.models.triple_barrier import _HORIZON_BARRIERS
        from qsde.execution.paper_journal import _HORIZON_SESSIONS
        for h in ("intraday", "swing", "long"):
            assert int(_HORIZON_BARRIERS[h]["t1_sessions"]) == \
                   int(_HORIZON_SESSIONS[h]), (
                f"{h}: label window ({_HORIZON_BARRIERS[h]['t1_sessions']}) "
                f"must equal paper window ({_HORIZON_SESSIONS[h]}) — H2 fix "
                f"is meaningless if these drift."
            )

    def test_get_t1_barriers_steps_by_session_not_calendar(self):
        """The load-bearing test: t1 for a Wednesday event with num_sessions=5
        must land on the following Wednesday (~7 calendar days), NOT on
        the Monday five calendar days after (which is only 3 sessions)."""
        from qsde.models.triple_barrier import get_t1_barriers
        # 15 business-day index: 2024-01-01 (Mon) through 2024-01-19 (Fri).
        idx = pd.bdate_range(start="2024-01-01", periods=15)
        close = pd.Series(range(15), index=idx, dtype=float)
        # Event on 2024-01-03 (Wed). 5 sessions later:
        # Wed 03 -> Thu 04 -> Fri 05 -> Mon 08 -> Tue 09 -> WED 10.
        # Calendar-days approach would land on Mon 08 (only 3 sessions).
        t_events = pd.DatetimeIndex([pd.Timestamp("2024-01-03")])
        t1 = get_t1_barriers(close, t_events, num_sessions=5)
        assert t1.iloc[0] == pd.Timestamp("2024-01-10"), (
            f"H2 regressed: t1 landed on {t1.iloc[0]}, expected 2024-01-10 "
            f"(5 sessions after 2024-01-03 Wed)."
        )

    def test_get_t1_barriers_signature_uses_num_sessions(self):
        """Signature-level check that the argument name reflects the fix."""
        from qsde.models.triple_barrier import get_t1_barriers
        sig = inspect.signature(get_t1_barriers)
        assert "num_sessions" in sig.parameters, (
            "get_t1_barriers must accept `num_sessions`, not `num_days` "
            "(H2 fix)."
        )

    def test_t1_beyond_index_returns_nat(self):
        """Events near the tail of the price series can't have a t1 — must
        gracefully return NaT."""
        from qsde.models.triple_barrier import get_t1_barriers
        idx = pd.bdate_range(start="2024-01-01", periods=10)
        close = pd.Series(range(10), index=idx, dtype=float)
        # Last event has no room for 5 more sessions.
        t_events = pd.DatetimeIndex([idx[-2]])   # only 1 session forward available
        t1 = get_t1_barriers(close, t_events, num_sessions=5)
        assert pd.isna(t1.iloc[0])


# ─────────────────────────────────────────────────────────────────────
# H4 — high/low + pessimistic tie
# ─────────────────────────────────────────────────────────────────────

def _bars(n: int, close_series: list[float],
          high_series: list[float] | None = None,
          low_series: list[float] | None = None) -> pd.DataFrame:
    """Build an OHLC panel from lists (indexed by business days)."""
    idx = pd.bdate_range(start="2024-01-01", periods=n)
    close = np.array(close_series, dtype=float)
    high = np.array(high_series if high_series else close, dtype=float)
    low = np.array(low_series if low_series else close, dtype=float)
    return pd.DataFrame({"high": high, "low": low, "close": close}, index=idx)


class TestH4HighLowDetection:
    def test_pt_hits_when_high_exceeds_threshold_but_close_does_not(self):
        """The scenario the H4 fix corrects: on day 1 the HIGH tags the
        profit-take level intraday, then the close comes back down. Under
        close-only detection, PT never fired; the label came out 0. Under
        the fix, PT fired on day 1."""
        from qsde.models.triple_barrier import _apply_pt_sl_on_t1
        # Entry at 100. Day 1: high=105 (PT would be 5% here), close=100.
        # Day 2: nothing. PT threshold = 5% * 1σ.
        bars = _bars(
            n=3,
            close_series=[100.0, 100.0, 100.0],
            high_series =[100.0, 105.0, 100.0],   # PT tagged intraday on day 1
            low_series  =[100.0, 100.0, 100.0],
        )
        events = pd.DataFrame({
            "t1":   [bars.index[2]],
            "trgt": [0.01],        # σ = 1%
            "side": [1.0],
        }, index=[bars.index[0]])
        out = _apply_pt_sl_on_t1(bars, events, pt_sl=(5.0, 5.0))
        assert pd.notna(out.at[bars.index[0], "pt"]), (
            "H4 regressed: PT must fire when HIGH crosses the barrier, "
            "even if the close doesn't."
        )
        assert out.at[bars.index[0], "pt"] == bars.index[1], (
            "PT must fire on the exact bar where high crossed."
        )

    def test_sl_hits_when_low_undercuts_threshold_but_close_does_not(self):
        """Symmetric H4 case on the loss side."""
        from qsde.models.triple_barrier import _apply_pt_sl_on_t1
        bars = _bars(
            n=3,
            close_series=[100.0, 100.0, 100.0],
            high_series =[100.0, 100.0, 100.0],
            low_series  =[100.0, 95.0,  100.0],   # SL tagged intraday on day 1
        )
        events = pd.DataFrame({
            "t1":   [bars.index[2]],
            "trgt": [0.01],
            "side": [1.0],
        }, index=[bars.index[0]])
        out = _apply_pt_sl_on_t1(bars, events, pt_sl=(5.0, 5.0))
        assert pd.notna(out.at[bars.index[0], "sl"])
        assert out.at[bars.index[0], "sl"] == bars.index[1]

    def test_pessimistic_same_bar_tie_favors_sl(self):
        """When PT and SL are both crossed by the same bar (high and low
        both reach their thresholds intraday), the pessimistic tie rule
        says SL wins — matching paper_journal._resolve_barrier_daily."""
        from qsde.models.triple_barrier import _apply_pt_sl_on_t1
        # Wild bar: high=105, low=95, close=100. Both barriers tagged.
        bars = _bars(
            n=3,
            close_series=[100.0, 100.0, 100.0],
            high_series =[100.0, 105.0, 100.0],
            low_series  =[100.0, 95.0,  100.0],
        )
        events = pd.DataFrame({
            "t1":   [bars.index[2]],
            "trgt": [0.01],
            "side": [1.0],
        }, index=[bars.index[0]])
        out = _apply_pt_sl_on_t1(bars, events, pt_sl=(5.0, 5.0))
        # SL wins; PT is nullified.
        assert pd.notna(out.at[bars.index[0], "sl"])
        assert pd.isna(out.at[bars.index[0], "pt"]), (
            "H4 pessimistic tie regressed: when PT and SL both hit on the "
            "same bar, SL must win (matches paper_journal)."
        )

    def test_short_side_uses_mirrored_thresholds(self):
        """SHORT (side=-1): PT hits when price MOVES DOWN (low tags), SL
        hits when price MOVES UP (high tags). Mirror of long."""
        from qsde.models.triple_barrier import _apply_pt_sl_on_t1
        bars = _bars(
            n=3,
            close_series=[100.0, 100.0, 100.0],
            high_series =[100.0, 100.0, 100.0],
            low_series  =[100.0, 95.0,  100.0],   # LOW is favorable for shorts
        )
        events = pd.DataFrame({
            "t1":   [bars.index[2]],
            "trgt": [0.01],
            "side": [-1.0],           # SHORT
        }, index=[bars.index[0]])
        out = _apply_pt_sl_on_t1(bars, events, pt_sl=(5.0, 5.0))
        # PT (short) fires when low undercuts entry.
        assert pd.notna(out.at[bars.index[0], "pt"])
        assert out.at[bars.index[0], "pt"] == bars.index[1]

    def test_strict_first_wins_no_same_bar_still_holds(self):
        """When PT and SL hit on DIFFERENT bars, first-strictly-wins —
        the pessimistic-tie rule only kicks in for same-bar collisions."""
        from qsde.models.triple_barrier import _apply_pt_sl_on_t1
        # Day 1: PT hit. Day 2: SL hit. PT is strictly first — must win.
        bars = _bars(
            n=4,
            close_series=[100.0, 100.0, 100.0, 100.0],
            high_series =[100.0, 105.0, 100.0, 100.0],   # PT day 1
            low_series  =[100.0, 100.0, 95.0,  100.0],   # SL day 2
        )
        events = pd.DataFrame({
            "t1":   [bars.index[3]],
            "trgt": [0.01],
            "side": [1.0],
        }, index=[bars.index[0]])
        out = _apply_pt_sl_on_t1(bars, events, pt_sl=(5.0, 5.0))
        assert out.at[bars.index[0], "pt"] == bars.index[1]
        assert out.at[bars.index[0], "sl"] == bars.index[2]


# ─────────────────────────────────────────────────────────────────────
# Integration — apply_triple_barrier_labels honors both fixes
# ─────────────────────────────────────────────────────────────────────

class TestApplyTripleBarrierIntegration:
    def _panel(self):
        """Build a symbol-panel where H4 vs close-only would give DIFFERENT
        labels — high tags PT on day 1 intraday, close drifts back."""
        n = 30
        idx = pd.bdate_range(start="2024-01-01", periods=n)
        # Build a series where volatility is ~1% via small daily noise, then
        # inject a big HIGH spike on day 10 for our synthetic event.
        rng = np.random.default_rng(7)
        close = 100.0 + np.cumsum(rng.normal(0, 0.5, n))
        high = close.copy()
        low = close.copy()
        # On day 10, high spikes 10% above; low stays flat.
        high[10] = close[10] * 1.10
        # Ensure high >= close and low <= close everywhere.
        high = np.maximum(high, close)
        low = np.minimum(low, close)
        ohlcv = pd.DataFrame({
            "symbol": "TEST",
            "date":   idx,
            "high":   high,
            "low":    low,
            "close":  close,
        })
        return ohlcv, idx

    def test_apply_labels_accepts_high_low_and_produces_labels(self):
        from qsde.models.triple_barrier import apply_triple_barrier_labels
        ohlcv, idx = self._panel()
        # Event on day 10, where the high spike happened.
        dataset = pd.DataFrame({
            "symbol": ["TEST"],
            "as_of_date": [idx[10]],
            "target": [0.0],   # will be replaced
        })
        # Use a short vol_span so σ stabilizes quickly on a small panel
        # — this is a hermetic test, not a realistic training run.
        out = apply_triple_barrier_labels(
            dataset, ohlcv, horizon="swing", vol_span=10,
        )
        assert not out.empty, "Should produce at least one labelled row"
        assert set(out["target"].unique()).issubset({-1, 0, 1})

    def test_apply_labels_warns_when_high_low_missing(self, caplog):
        """The docstring says ohlcv now needs high+low. If a caller passes
        an old close-only panel, we degrade gracefully with a warning."""
        from qsde.models.triple_barrier import apply_triple_barrier_labels
        import logging
        n = 30
        idx = pd.bdate_range(start="2024-01-01", periods=n)
        ohlcv = pd.DataFrame({
            "symbol": "TEST",
            "date":   idx,
            "close":  np.linspace(100.0, 105.0, n),
        })
        dataset = pd.DataFrame({
            "symbol": ["TEST"],
            "as_of_date": [idx[10]],
            "target": [0.0],
        })
        with caplog.at_level(logging.WARNING):
            _ = apply_triple_barrier_labels(dataset, ohlcv, horizon="swing")
        # Any warning about missing high/low columns is what we're asserting.
        msgs = [rec.message for rec in caplog.records]
        assert any(("missing" in m.lower() and ("high" in m.lower() or "low" in m.lower()))
                   or "H4" in m
                   for m in msgs), (
            "When ohlcv lacks high/low, must log a warning naming either "
            "the missing dim or the H4 fix so the caller knows their labels "
            "are degraded."
        )
