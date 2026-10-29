"""
Tests for the DSR promotion gate (qsde/models/deflated_sharpe.should_promote)
and the BSE universe baseline. Pure / DB-free.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from qsde.models.deflated_sharpe import (
    should_promote, validate_strategy, deflated_sharpe_ratio,
)
from qsde.ingestion.universe import build_bse_universe, BSE_SENSEX_SYMBOLS


def test_promotion_is_paused_by_default(monkeypatch):
    """The legacy gate (close-of-t labels, subsampled periods, n_trials=1) is superseded by qsde.ml.eval:
    nothing may be promoted on it unless explicitly re-enabled."""
    monkeypatch.delenv("QSDE_ML_PROMOTION_ENABLED", raising=False)
    monkeypatch.delenv("QSDE_FORCE_PROMOTE", raising=False)
    g = should_promote(0.99, threshold=0.95, force=False)
    assert g["paused"] and not g["promote"]
    monkeypatch.setenv("QSDE_FORCE_PROMOTE", "true")
    assert not should_promote(0.10)["promote"]            # even a force flag cannot override the pause
    monkeypatch.setenv("QSDE_ML_PROMOTION_ENABLED", "true")
    assert should_promote(0.10)["promote"]                # explicit research override re-enables it


def test_should_promote_threshold(monkeypatch):
    monkeypatch.setenv("QSDE_ML_PROMOTION_ENABLED", "true")          # threshold logic under test
    g = should_promote(0.96, threshold=0.95, force=False)
    assert g["promote"] and g["passed"] and not g["forced"]

    g = should_promote(0.50, threshold=0.95, force=False)
    assert not g["promote"] and not g["passed"] and not g["forced"]

    g = should_promote(0.50, threshold=0.95, force=True)
    assert g["promote"] and g["forced"] and not g["passed"]

    g = should_promote(0.40, threshold=0.30, force=False)
    assert g["promote"] and g["passed"]

    g = should_promote(None, threshold=0.95, force=False)
    assert not g["promote"]


def test_dsr_deflates_with_more_trials():
    psr = deflated_sharpe_ratio(1.5, n_trials=1, n_obs=200)      # collapses to PSR
    dsr_many = deflated_sharpe_ratio(1.5, n_trials=100, n_obs=200)
    assert 0.0 <= dsr_many <= 1.0
    assert dsr_many <= psr + 1e-9   # deflation never increases the score


def test_validate_strategy_keys_and_ranges():
    rng = np.random.default_rng(0)
    rets = rng.normal(0.001, 0.01, 300)
    v = validate_strategy(rets, n_trials=1)
    for k in ["sharpe", "deflated_sharpe", "psr", "skewness", "kurtosis",
              "n_observations", "pass_dsr", "pass_psr"]:
        assert k in v
    assert 0.0 <= v["deflated_sharpe"] <= 1.0
    assert 0.0 <= v["psr"] <= 1.0


def test_build_bse_universe():
    df = build_bse_universe()
    assert len(df) == len(BSE_SENSEX_SYMBOLS)
    assert (df["exchange"] == "BSE").all()
    assert "BSE SENSEX" in df["index_membership"].iloc[0]
    assert df["is_active"].all()


class TestLegacyTrainerRetired:
    def test_legacy_trainer_refuses_to_run_by_default(self, monkeypatch):
        import pandas as pd
        import pytest

        from qsde.models.lgbm_model import train_lightgbm_model
        monkeypatch.delenv("QSDE_ALLOW_LEGACY_TRAINER", raising=False)
        with pytest.raises(RuntimeError, match="retrain.py"):
            train_lightgbm_model(pd.DataFrame(), horizon="swing")

    def test_retrain_review_gate(self):
        import importlib.util
        from pathlib import Path

        spec = importlib.util.spec_from_file_location(
            "retrain_script", Path(__file__).resolve().parents[1] / "scripts" / "retrain.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        full = "adv60_top500_v1+adv10+full"
        assert mod.review_gate("snap-1", full, None, False) is not None          # unreviewed full universe blocked
        assert mod.review_gate("snap-1", full, "snap-OTHER", False) is not None   # reviewed a different snapshot
        assert mod.review_gate("snap-1", full, "snap-1", True) is None            # reviewed: proceed
        assert mod.review_gate("snap-1", "x+PILOT-SURVIVOR-SELECTED", None, False) is None      # plumbing is free
        assert mod.review_gate("snap-1", "x+PILOT-SURVIVOR-SELECTED", None, True) is not None   # but never a candidate
