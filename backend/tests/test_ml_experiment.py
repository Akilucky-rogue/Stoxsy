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


def test_constant_features_are_excluded_from_model_inputs(world):
    prices, feats, _ = world
    f = feats.copy()
    f["smc_dead_zone"] = 0.0
    snap = type("S", (), {"snapshot_id": "s", "universe_version": "SYNTHETIC-w", "dataset_hash": "ab" * 32})()
    ds = build_labelled_dataset("swing_5d", feats=f, prices=prices, exclusions=pd.DataFrame(columns=["symbol", "ex_date"]),
                                snapshot=snap)
    assert "smc_dead_zone" not in ds.feature_cols and any("smc_dead_zone" in n for n in ds.notes)


def test_entry_contract_holds_on_labels_and_flags_a_tampered_one(world):
    from qsde.ml.contract import check_entry_contract
    ds = _ds(world)
    res = check_entry_contract(ds, n=150)
    assert res["n_checked"] >= 100 and res["failures"] == []
    ds.frame.loc[ds.frame.index[:200], "entry"] = ds.frame.loc[ds.frame.index[:200], "entry"] * 1.5   # entry that is not the open
    bad = check_entry_contract(ds, n=len(ds.frame))
    assert bad["failures"], "a label with a non-open entry must be reported"


def test_model_frame_gives_trees_raw_and_linear_models_ranked_heavy_tails(world):
    prices, feats, _ = world
    f = feats.copy()
    f["smc_demo_dist"] = np.random.default_rng(1).normal(size=len(f))
    f.loc[f.index[::2500], "smc_demo_dist"] = 5000.0
    snap = type("S", (), {"snapshot_id": "s", "universe_version": "SYNTHETIC-w", "dataset_hash": "ab" * 32})()
    ds = build_labelled_dataset("swing_5d", feats=f, prices=prices, exclusions=pd.DataFrame(columns=["symbol", "ex_date"]),
                                snapshot=snap)
    raw, rep_t = ds.model_frame("tree")
    lin, rep_l = ds.model_frame("linear")
    assert rep_t["transformed"] == [] and raw["smc_demo_dist"].max() >= 5000.0
    assert "smc_demo_dist" in rep_l["transformed"] and lin["smc_demo_dist"].abs().max() <= 4.0


# ── cross-sectional target + null control ────────────────────────────────────

def test_cross_sectional_target_is_per_date_rank_gauss(world):
    from qsde.ml.experiment import cross_sectional_target
    lab = _ds(world).frame
    z = cross_sectional_target(lab)
    f = lab[["as_of_date"]].assign(z=z, r=lab["ret_net"])
    assert np.isfinite(z).all()
    assert f.groupby("as_of_date")["z"].mean().abs().max() < 0.05           # no market-level component left
    # monotone within a date: a higher raw return never gets a lower score
    d = f[f["as_of_date"] == f["as_of_date"].iloc[0]].sort_values("r")
    assert d["z"].is_monotonic_increasing


def test_null_control_is_reported_on_the_card_and_is_near_zero(world, tmp_path):
    r = run_experiment(_ds(world), ridge_fit_predict(), "ridge", {}, ledger_dir=tmp_path, groups=6, k=2, baseline_seeds=2)
    nc = r["null_control"]
    assert nc["ok"] and abs(nc["null_ic_mean"]) < nc["limit"] and len(nc["null_ic_paths"]) >= 1
    assert "null control" in r["card"] and "training target: `cs_rank_gauss`" in r["card"]


def test_a_biased_null_downgrades_the_run_to_plumbing(world, tmp_path, monkeypatch):
    from qsde.ml import experiment as ex
    monkeypatch.setattr(ex, "null_control", lambda *a, **k: {"null_ic_mean": 0.05, "null_ic_paths": [0.05], "ic_vs_true": 0.0,
                                                             "limit": 0.015, "ok": False})
    r = ex.run_experiment(_ds(world, uv="uni-null"), ridge_fit_predict(), "ridge", {}, ledger_dir=tmp_path, groups=6, k=2, baseline_seeds=2)
    assert r["median"]["verdict_allowed"] is False
    assert any("null control biased" in x for x in r["median"]["evidence_reasons"])


def test_unknown_target_is_rejected(world, tmp_path):
    with pytest.raises(ValueError):
        run_experiment(_ds(world), ridge_fit_predict(), "ridge", {}, ledger_dir=tmp_path, target="nope")


def test_atr_neutral_target_has_no_linear_exposure_to_the_control(world):
    from qsde.ml.experiment import atr_neutral_target
    lab = _ds(world).frame.copy()
    lab["ctrl"] = lab.groupby("as_of_date")["ret_net"].rank(pct=True) * 0.7 + np.random.default_rng(1).normal(0, 0.3, len(lab))
    y = atr_neutral_target(lab, ctrl="ctrl")
    f = lab[["as_of_date"]].assign(y=y, r=lab.groupby("as_of_date")["ctrl"].rank(pct=True))
    cors = f.groupby("as_of_date").apply(lambda g: g["y"].corr(g["r"]), include_groups=False)
    assert cors.abs().max() < 1e-8                                           # orthogonal to the control on every date
    assert np.isfinite(y).all()


def test_non_equity_instruments_are_dropped_and_recorded_in_the_universe_version(world):
    prices, feats, _ = world
    extra = feats[feats["symbol"] == feats["symbol"].iloc[0]].copy()
    extra["symbol"] = "LIQUIDCASE"                                                    # a liquid-fund unit that traded in the EQ series
    p2 = pd.concat([prices, prices[prices["symbol"] == prices["symbol"].iloc[0]].assign(symbol="LIQUIDCASE")])
    snap = type("S", (), {"snapshot_id": "snap-test", "universe_version": "uni-x", "dataset_hash": "ab" * 32})()
    kw = dict(feats=pd.concat([feats, extra]), prices=p2, exclusions=pd.DataFrame(columns=["symbol", "ex_date"]), snapshot=snap)
    ds = build_labelled_dataset("swing_5d", **kw)
    assert "LIQUIDCASE" not in set(ds.frame["symbol"]) and "|ex_non_equity_v1:" in ds.universe_version
    assert any("non-equity instruments excluded" in n for n in ds.notes)
    ds2 = build_labelled_dataset("swing_5d", exclude_non_equity=False, **kw)
    assert "LIQUIDCASE" in set(ds2.frame["symbol"]) and "ex_non_equity" not in ds2.universe_version
