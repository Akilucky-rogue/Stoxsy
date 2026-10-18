"""Dataset builder + experiment runner: ledger, PBO, price-policy gating (synthetic world, no DB)."""
import numpy as np
import pandas as pd
import pytest

from qsde.ml import data as mldata
from qsde.ml.dataset import build_labelled_dataset
from qsde.ml.experiment import run_experiment, trial_key
from qsde.ml.ledger import load_trials, trial_count
from qsde.ml.selftest import make_panel, ridge_fit_predict


@pytest.fixture(scope="module")
def world():
    return make_panel(n_sym=30, n_days=420, edge_bps=40.0, seed=3)


def _ds(world, policy=mldata.OFFICIAL, uv="SYNTHETIC-world"):
    prices, feats, _ = world
    snap = type("S", (), {"snapshot_id": "snap-test", "universe_version": uv, "dataset_hash": "ab" * 32})()
    return build_labelled_dataset("swing_5d", feats=feats, prices=prices, exclusions=pd.DataFrame(columns=["symbol", "ex_date"]),
                                  policy=policy, snapshot=snap)


def test_dataset_carries_registry_labels_and_provenance(world):
    ds = _ds(world)
    assert ds.spec.id == "swing_5d" and ds.frame["label_id"].eq("tb_nextopen_v1").all()
    assert {"ret_net", "label", "label_end_session", "entry_date", "r1", "r5"} <= set(ds.frame.columns)
    assert (ds.frame["entry_date"] > ds.frame["as_of_date"]).all()          # next-open entry, never the signal close
    assert set(ds.feature_cols) == {"x_signal", "noise_a", "noise_b"}
    assert ds.meta()["price_policy"] == "official" and ds.adjustment_complete


def test_experiment_appends_a_trial_each_call_and_reports_pbo_after_two(world, tmp_path):
    ds = _ds(world)
    key = trial_key(ds)
    r1 = run_experiment(ds, ridge_fit_predict(), "ridge", {"alpha": 10}, ledger_dir=tmp_path, groups=6, k=2,
                        baseline_seeds=2)
    assert trial_count(key, tmp_path) == 1 and r1["pbo"] is None
    r2 = run_experiment(ds, ridge_fit_predict(100.0), "ridge", {"alpha": 100}, ledger_dir=tmp_path, groups=6, k=2,
                        baseline_seeds=2)
    assert trial_count(key, tmp_path) == 2 and r2["median"]["dsr"]["n_trials"] == 2
    assert r2["pbo"] is None or 0.0 <= r2["pbo"] <= 1.0


def test_synthetic_universe_never_gets_a_verdict(world, tmp_path):
    r = run_experiment(_ds(world), ridge_fit_predict(), "ridge", {}, ledger_dir=tmp_path, groups=6, k=2, baseline_seeds=2)
    assert r["verdict"] == "NOT_ASSESSED" and "PLUMBING ONLY" in r["card"].upper()


def test_research_policy_is_forced_to_plumbing_with_a_reason_and_a_separate_trial_key(world, tmp_path):
    off, res = _ds(world, uv="uni-x"), _ds(world, policy=mldata.RESEARCH, uv="uni-x")
    assert trial_key(off) != trial_key(res)
    r = run_experiment(res, ridge_fit_predict(), "ridge", {}, ledger_dir=tmp_path, groups=6, k=2, baseline_seeds=2)
    assert r["median"]["verdict_allowed"] is False and r["verdict"] == "NOT_ASSESSED"
    assert any("adjustment incomplete" in x for x in r["median"]["evidence_reasons"])
    assert "adjustment is incomplete" in r["card"]


def test_failed_fit_is_still_counted_as_a_trial(world, tmp_path):
    ds = _ds(world)

    def boom(*a):
        raise RuntimeError("fit exploded")

    with pytest.raises(RuntimeError):
        run_experiment(ds, boom, "boom", {}, ledger_dir=tmp_path, groups=6, k=2)
    t = load_trials(trial_key(ds), tmp_path)
    assert len(t) == 1 and t[0]["status"] == "failed"
