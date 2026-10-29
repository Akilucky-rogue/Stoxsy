"""Regression tests for Stage 2C.3 of Session 2 (AUDIT_v2 C3).

C3 bug: Promotion DSR was on GROSS returns; costs never entered the gate.
Docs claimed "cost-aware target" but under the default triple_barrier
label path, the cost subtraction was dead code.

Current state (verified in Stage 2C.3):
  * Promotion gate uses dsr_net via _evaluate_oos_with_costs (already
    fixed in tree before this stage; tests here LOCK it as regression
    guard).
  * apply_triple_barrier_labels accepts an opt-in cost_bps > 0 that
    shifts PT and SL barriers by cost_frac so a +1 label means the
    trade cleared its target NET of cost. Off by default because the
    promotion gate already carries the load-bearing net check.
  * Docstrings in retrain.py and README.md corrected to reflect reality.

Hermetic — no DB, no network. Source-inspection + synthetic OHLC.
"""
from __future__ import annotations

import inspect
from pathlib import Path

import numpy as np
import pandas as pd
import pytest


class TestPromotionGateOnNetDSR:
    """The load-bearing regression guard: `train_lightgbm_model` must gate
    on `dsr_net`, not `dsr_gross`."""

    def test_train_calls_evaluate_oos_with_costs(self):
        from qsde.models import lgbm_model
        src = Path(lgbm_model.__file__).read_text()
        assert "_evaluate_oos_with_costs" in src, (
            "C3 gate regressed: train_lightgbm_model must call "
            "_evaluate_oos_with_costs and gate on its dsr_net."
        )

    def test_gate_uses_dsr_net_not_gross(self):
        from qsde.models import lgbm_model
        src = Path(lgbm_model.__file__).read_text()
        # The pattern we're guarding: `should_promote(dsr_net)`.
        assert "should_promote(dsr_net)" in src, (
            "C3 gate regressed: should_promote() must be called with "
            "dsr_net, not dsr_gross or metrics['dsr']. Gross gating is "
            "exactly the bug AUDIT_v2 C3 flagged."
        )

    def test_promotion_cost_bps_env_var_wired(self):
        from qsde.models import lgbm_model
        src = Path(lgbm_model.__file__).read_text()
        assert "QSDE_PROMOTION_COST_BPS" in src, (
            "QSDE_PROMOTION_COST_BPS env var must drive the cost "
            "assumption for the promotion gate."
        )


class TestOptInCostAwareBarriers:
    """Stage 2C.3 adds opt-in cost-aware barriers to triple_barrier.
    Verify the API + the semantics."""

    def test_apply_labels_accepts_cost_bps(self):
        from qsde.models.triple_barrier import apply_triple_barrier_labels
        sig = inspect.signature(apply_triple_barrier_labels)
        assert "cost_bps" in sig.parameters
        assert sig.parameters["cost_bps"].default == 0.0, (
            "Default MUST be 0 — cost-aware labels are opt-in. Turning "
            "them on by default would change the label distribution "
            "and require re-tuning every downstream threshold."
        )

    def test_get_events_accepts_cost_frac(self):
        from qsde.models.triple_barrier import get_events
        sig = inspect.signature(get_events)
        assert "cost_frac" in sig.parameters
        assert sig.parameters["cost_frac"].default == 0.0

    def test_cost_bps_zero_preserves_baseline_behavior(self):
        """cost_bps=0 must give identical results to the pre-2C.3 code."""
        from qsde.models.triple_barrier import _apply_pt_sl_on_t1
        n = 4
        idx = pd.bdate_range(start="2024-01-01", periods=n)
        # Bar 1: high tags +5% (PT for pt_mult*sigma = 5 * 0.01 = 5%).
        bars = pd.DataFrame({
            "high":  [100.0, 105.0, 100.0, 100.0],
            "low":   [100.0, 100.0, 100.0, 100.0],
            "close": [100.0, 100.0, 100.0, 100.0],
        }, index=idx)
        events = pd.DataFrame({
            "t1":   [idx[3]],
            "trgt": [0.01],   # sigma = 1%
            "side": [1.0],
        }, index=[idx[0]])
        # With cost_frac=0, PT fires at bar 1 (5% high >= 5% * 1σ + 0).
        out = _apply_pt_sl_on_t1(bars, events, pt_sl=(5.0, 5.0), cost_frac=0.0)
        assert pd.notna(out.at[idx[0], "pt"])

    def test_positive_cost_frac_delays_pt(self):
        """With cost_frac > 0, PT threshold is higher — a bar that
        JUST tagged the gross threshold no longer clears the net one."""
        from qsde.models.triple_barrier import _apply_pt_sl_on_t1
        n = 4
        idx = pd.bdate_range(start="2024-01-01", periods=n)
        # Bar 1: high = 100 * (1 + 5%) = 105 exactly the gross PT.
        bars = pd.DataFrame({
            "high":  [100.0, 105.0, 100.0, 100.0],
            "low":   [100.0, 100.0, 100.0, 100.0],
            "close": [100.0, 100.0, 100.0, 100.0],
        }, index=idx)
        events = pd.DataFrame({
            "t1":   [idx[3]],
            "trgt": [0.01],
            "side": [1.0],
        }, index=[idx[0]])
        # cost_frac = 0.005 (50 bps): effective PT threshold = 5% + 0.5% = 5.5%.
        # The 5% tag on bar 1 no longer fires PT.
        out = _apply_pt_sl_on_t1(bars, events, pt_sl=(5.0, 5.0), cost_frac=0.005)
        assert pd.isna(out.at[idx[0], "pt"]), (
            "With positive cost_frac, a bar that only tags the GROSS PT "
            "threshold must NOT fire — the net-of-cost target requires "
            "additional move."
        )

    def test_positive_cost_frac_delays_sl(self):
        """Symmetric: SL fires further away when cost_frac > 0."""
        from qsde.models.triple_barrier import _apply_pt_sl_on_t1
        n = 4
        idx = pd.bdate_range(start="2024-01-01", periods=n)
        # Bar 1: low = 100 * (1 - 5%) = 95 exactly the gross SL.
        bars = pd.DataFrame({
            "high":  [100.0, 100.0, 100.0, 100.0],
            "low":   [100.0, 95.0,  100.0, 100.0],
            "close": [100.0, 100.0, 100.0, 100.0],
        }, index=idx)
        events = pd.DataFrame({
            "t1":   [idx[3]],
            "trgt": [0.01],
            "side": [1.0],
        }, index=[idx[0]])
        # Effective SL = -5% - 0.5% = -5.5%; a 5% low doesn't fire.
        out = _apply_pt_sl_on_t1(bars, events, pt_sl=(5.0, 5.0), cost_frac=0.005)
        assert pd.isna(out.at[idx[0], "sl"])


class TestDocsCorrected:
    def test_retrain_docstring_no_longer_claims_cost_aware_target(self):
        from pathlib import Path
        p = Path(__file__).resolve().parents[2] / "backend" / "scripts" / "retrain.py"
        src = p.read_text()
        # The old lie: "models that predict NET-of-cost returns natively"
        assert "NET-of-cost returns natively" not in src, (
            "retrain.py docstring must not claim models predict "
            "net-of-cost returns natively — under triple_barrier that "
            "was never true. AUDIT_v2 C3."
        )
        # And the correction must reference the gate, not the target.
        assert "promotion gate" in src.lower() or "promotion_cost_bps" in src.lower(), (
            "retrain.py docstring must explain that costs enter at the "
            "promotion gate (dsr_net), not in the labels."
        )

    def test_readme_no_longer_claims_cost_aware_target_bullet(self):
        from pathlib import Path
        p = Path(__file__).resolve().parents[2] / "README.md"
        src = p.read_text()
        assert "cost is subtracted from labels before training" not in src, (
            "README.md must not carry the false 'cost subtracted from labels' "
            "claim. AUDIT_v2 C3."
        )
        # And the corrected bullet must mention dsr_net / promotion gate.
        assert "dsr_net" in src or "promotion gate" in src.lower(), (
            "README.md must reflect the ACTUAL cost handling — dsr_net "
            "at the promotion gate."
        )


class TestC3LoadBearingProperty:
    """One-stop check that gross-Sharpe promotions are no longer possible."""

    def test_no_direct_should_promote_on_gross_dsr(self):
        """Nowhere in the ML training code should `should_promote` be
        called on a gross DSR value. `should_promote(dsr_net)` is the
        only sanctioned pattern."""
        from qsde.models import lgbm_model
        src = Path(lgbm_model.__file__).read_text()
        # Look for accidental should_promote(metrics["dsr"]) or dsr_gross.
        assert "should_promote(metrics" not in src, (
            "should_promote must be called on dsr_net, never on the raw "
            "gross metrics dict."
        )
        assert "should_promote(dsr_gross)" not in src, (
            "should_promote(dsr_gross) — that IS the C3 bug. Never."
        )
