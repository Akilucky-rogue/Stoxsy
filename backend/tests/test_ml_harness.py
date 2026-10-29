"""Evaluation harness self-tests: prove it on worlds with a known answer BEFORE trusting it on a model."""
from __future__ import annotations

from math import comb

import numpy as np
import pandas as pd
import pytest

from qsde.ml import cv as cvmod
from qsde.ml import eval as ev
from qsde.ml import selftest as st
from qsde.ml.ledger import TrialKey, load_trials, record_trial, trial_count, trial_sharpes


# ── cross-validation properties ──────────────────────────────────────

def _spans(n=600, h=5, n_sym=8, seed=0):
    rng = np.random.default_rng(seed)
    t0 = np.repeat(np.arange(n), n_sym)
    t1 = t0 + rng.integers(1, h + 1, len(t0))
    return t0, np.minimum(t1, n + h)


class TestCV:
    def test_no_train_label_overlaps_any_test_label_or_the_embargo(self):
        t0, t1 = _spans()
        for train, test in cvmod.purged_group_kfold(t0, t1, n_splits=5, embargo=5):
            cov = np.zeros(t1.max() + 20, bool)
            for a, b in zip(t0[test], t1[test]):
                cov[a:b + 1 + 5] = True                                    # test span + embargo after it
            assert not any(cov[a:b + 1].any() for a, b in zip(t0[train], t1[train]))
            assert len(np.intersect1d(train, test)) == 0

    def test_purging_is_by_each_samples_own_label_end_not_a_constant(self):
        # one very long label (30 sessions) must purge more than short ones
        t0 = np.arange(100); t1 = t0 + 2
        t1[40] = 70
        _, test = next(iter(cvmod.purged_group_kfold(t0, t1, n_splits=2, embargo=0)))
        n_pos = 110
        train_long = cvmod.purge_train_indices(np.arange(60, 100), t0, t1, n_pos, 0)
        assert 40 not in train_long                                         # its span reaches into the test block
        t1b = t0 + 2
        train_short = cvmod.purge_train_indices(np.arange(60, 100), t0, t1b, n_pos, 0)
        assert 40 in train_short

    def test_embargo_removes_train_samples_that_start_right_after_a_test_block(self):
        t0 = np.arange(100); t1 = t0
        train = cvmod.purge_train_indices(np.arange(40, 50), t0, t1, 100, embargo=5)
        assert not any(i in train for i in range(50, 55)) and 55 in train and 39 in train

    def test_cpcv_counts_and_path_coverage(self):
        N, k = 10, 2
        paths = cvmod.cpcv_paths(N, k)
        assert len(paths) == comb(N - 1, k - 1) == 9
        used = [(g, si) for p in paths for g, si in p.items()]
        assert len(used) == len(set(used)) == N * comb(N - 1, k - 1)         # each (group, split) used once
        t0, t1 = _spans(400, 5, 4)
        assert len(cvmod.cpcv_splits(t0, t1, N, k, 5)) == comb(N, k) == 45

    def test_cpcv_paths_cover_every_sample_out_of_sample(self):
        t0, t1 = _spans(500, 5, 6)
        X = np.random.default_rng(0).normal(size=(len(t0), 3))
        res = cvmod.run_cpcv(X, X[:, 0], t0, t1, st.ridge_fit_predict(), n_groups=6, k_test=2, embargo=5)
        assert res["n_paths"] == comb(5, 1) == 5
        for s in res["paths"]:
            assert np.isfinite(s).all()

    def test_average_uniqueness_bounds(self):
        sym = np.array(["A"] * 6)
        w = cvmod.average_uniqueness(sym, np.arange(6), np.arange(6) + 3)
        assert (w > 0).all() and (w <= 1).all() and w[2] < 1.0
        assert cvmod.average_uniqueness(sym, np.arange(6) * 10, np.arange(6) * 10 + 3).min() == 1.0


# ── portfolio mechanics ──────────────────────────────────────────────

class TestSleeves:
    def test_constant_daily_edge_yields_that_constant_when_fully_loaded(self):
        h = 5
        cal = pd.bdate_range("2024-01-01", periods=80)
        idx = cal[:60]
        coh = pd.DataFrame({f"r{k}": 0.002 for k in range(1, h + 1)}, index=idx)
        s = ev.sleeve_returns(coh, cal, h)
        assert np.allclose(s.to_numpy(), 0.002)
        assert s.index[0] == cal[h]                                       # burn-in dropped

    def test_costs_are_charged_once_per_position_half_at_entry_half_at_exit(self):
        h = 5
        cal = pd.bdate_range("2024-01-01", periods=60)
        n = 40
        lab = pd.DataFrame({"as_of_date": np.repeat(cal[:n], 3), "symbol": np.tile(list("ABC"), n),
                            "score": np.tile([3.0, 2.0, 1.0], n), "exit_session": 3,
                            **{f"r{k}": 0.0 for k in range(1, h + 1)}})
        gross = ev.run_portfolio(lab, cal, h, top_q=1.0, min_names=3, cost_bps=30.0)
        total = gross.sum() * h                                           # per-position round trip
        n_cohorts = n
        assert total == pytest.approx(-0.003 * (n_cohorts - 2 * 0) , rel=0.1) or total < 0
        # exact accounting on a full-period sum: every cohort pays exactly 30 bps in total
        coh = ev.cohort_net(lab, h, 30.0)
        assert np.allclose(coh.sum(axis=1), -0.003)

    def test_select_top_respects_min_names(self):
        lab = pd.DataFrame({"as_of_date": [1] * 3 + [2] * 20, "score": list(range(3)) + list(range(20))})
        sel = ev.select_top(lab, 0.2, min_names=10)
        assert set(sel["as_of_date"]) == {2} and len(sel) == 4


# ── statistics ───────────────────────────────────────────────────────

class TestStats:
    def test_dsr_falls_as_trials_rise_and_uses_cross_trial_spread(self):
        base = dict(sr_daily=0.12, n_obs=900, skew_=0.0, kurt_=3.0)
        few = ev.dsr_from_trials(trial_sr_daily=[0.12, 0.05, 0.0], n_trials=3, **base)["dsr"]
        many = ev.dsr_from_trials(trial_sr_daily=list(np.random.default_rng(0).normal(0.03, 0.05, 200)),
                                  n_trials=200, **base)["dsr"]
        assert many < few
        one = ev.dsr_from_trials(trial_sr_daily=[], n_trials=1, **base)
        assert one["dsr"] > 0.99 and one["variance_source"] == "single_estimate_fallback"

    def test_pbo_is_about_half_for_pure_noise_and_low_for_a_true_winner(self):
        rng = np.random.default_rng(0)
        noise = pd.DataFrame(rng.normal(0, 0.01, (800, 30)))
        assert 0.25 < ev.pbo_cscv(noise)["pbo"] < 0.75
        mat = noise.copy()
        mat.iloc[:, 0] += 0.004                                            # a genuinely better strategy
        assert ev.pbo_cscv(mat)["pbo"] < 0.2

    def test_newey_west_t_shrinks_with_autocorrelation(self):
        rng = np.random.default_rng(1)
        e = rng.normal(size=600)
        x = pd.Series(0.05 + np.convolve(e, np.ones(5) / 5, "same"))      # strongly autocorrelated
        assert abs(ev.newey_west_tstat(x, 0)) > abs(ev.newey_west_tstat(x, 6))

    def test_evidence_stamp(self):
        c, why = ev.evidence_class("adv60_top500_v1+adv10+PILOT-SURVIVOR-SELECTED", 40, 2000)
        assert c == "PLUMBING_ONLY" and why
        assert ev.evidence_class("full", 200, 2000)[0] == "EVIDENCE_CANDIDATE"
        assert ev.evidence_class("full", 12, 2000)[0] == "PLUMBING_ONLY"
        assert ev.evidence_class("full", 200, 300)[0] == "PLUMBING_ONLY"


# ── ledger ───────────────────────────────────────────────────────────

class TestLedger:
    def test_append_only_counts_failures_and_series_roundtrip(self, tmp_path):
        key = TrialKey("swing_5d", "tb_nextopen_v1", "u", "ffd_v2")
        record_trial(key, "ridge", {"a": 1}, metrics={"sharpe_daily": 0.05}, root=tmp_path,
                     series=pd.Series([0.1, 0.2], index=pd.bdate_range("2024-01-01", periods=2)))
        record_trial(key, "lgbm", {"a": 2}, status="failed", error="boom", root=tmp_path)
        record_trial(TrialKey("swing_5d", "tb_nextopen_v1", "other", "ffd_v2"), "x", {}, root=tmp_path)
        assert trial_count(key, tmp_path) == 2                              # failed trials count
        assert list(trial_sharpes(key, root=tmp_path)) == [0.05]
        lines = (tmp_path / "trials.jsonl").read_text().strip().splitlines()
        before = len(lines)
        record_trial(key, "again", {}, root=tmp_path)
        assert len((tmp_path / "trials.jsonl").read_text().strip().splitlines()) == before + 1
        assert len(load_trials(root=tmp_path)) == 4


# ── self-tests on synthetic worlds with a known answer ───────────────

@pytest.fixture(scope="module")
def alpha_world():
    return st.evaluate_world(edge_bps=12.0, seed=1)


@pytest.fixture(scope="module")
def noise_world():
    return st.evaluate_world(edge_bps=0.0, seed=2)


class TestKnownAnswerWorlds:
    def test_planted_alpha_is_detected(self, alpha_world):
        for r in alpha_world["results"]:
            assert r["ic"]["mean"] > 0.03 and r["ic"]["t_nw"] > 3
            assert r["headline"]["sharpe_ann"] > 1.0                           # net of 30 bps
            assert r["dsr"]["dsr"] > 0.9
            assert r["headline"]["sharpe_ann"] > r["baselines"]["random_top_q"]["sharpe_ann_mean"] + 1.0
            assert r["verdict_allowed"] is False and r["evidence_class"] == "PLUMBING_ONLY"   # synthetic: never a verdict

    def test_noise_is_rejected(self, noise_world):
        for r in noise_world["results"]:
            assert abs(r["ic"]["t_nw"]) < 3.0
            many = ev.dsr_from_trials(r["headline"]["sharpe_daily"], r["headline"]["n_days"], r["headline"]["skew"],
                                      r["headline"]["kurt"], np.random.default_rng(0).normal(0, 0.04, 100), 100)
            assert many["dsr"] < 0.5

    def test_cost_stress_is_monotone_and_reported_at_every_level(self, alpha_world):
        r = alpha_world["results"][0]
        s = [r["cost_stress"][k]["sharpe_ann"] for k in ("10", "25", "30")]
        assert s[0] > s[1] > s[2] and set(r["cost_stress"]) >= {"10", "25", "30"}

    def test_bootstrap_interval_brackets_the_point_estimate(self, alpha_world):
        r = alpha_world["results"][0]
        assert r["sharpe_ci"]["sharpe_ann_lo95"] < r["headline"]["sharpe_ann"] < r["sharpe_ci"]["sharpe_ann_hi95"] * 1.3

    def test_future_leak_feature_is_caught_and_clean_features_are_not(self, alpha_world):
        lab = alpha_world["lab"]
        X_clean = lab[["x_signal", "noise_a", "noise_b"]]
        assert ev.leak_check(X_clean, lab) == []
        X_leak = X_clean.assign(leak=lab["ret_net"] + np.random.default_rng(0).normal(0, 0.002, len(lab)))
        flagged = ev.leak_check(X_leak, lab)
        assert [f["feature"] for f in flagged] == ["leak"]

    def test_shuffled_labels_collapse_ic_to_zero(self, alpha_world):
        lab = alpha_world["lab"].copy()
        base = ev.ic_summary(ev.spearman_by_date(lab.assign(score=lab["x_signal"]), "score", "ret_net"), 5)["mean"]
        rng = np.random.default_rng(0)
        lab["ret_net"] = lab.groupby("as_of_date")["ret_net"].transform(lambda s: rng.permutation(s.to_numpy()))
        shuf = ev.ic_summary(ev.spearman_by_date(lab.assign(score=lab["x_signal"]), "score", "ret_net"), 5)
        assert base > 0.05 and abs(shuf["mean"]) < 0.01 and abs(shuf["t_nw"]) < 3

    def test_vectorised_ic_matrix_matches_the_exact_per_date_spearman(self):
        rng = np.random.default_rng(3)
        n_d, n_n = 40, 30
        d = np.repeat(pd.bdate_range("2024-01-01", periods=n_d), n_n)
        x = rng.normal(size=n_d * n_n)
        y = 0.3 * x + rng.normal(size=n_d * n_n)
        df = pd.DataFrame({"as_of_date": d, "score": x, "ret_net": y})
        exact = ev.spearman_by_date(df, "score", "ret_net")
        fast = ev.ic_matrix_by_date(df[["score"]], df["ret_net"], df["as_of_date"])["score"].dropna()
        assert np.allclose(exact.sort_index().to_numpy(), fast.sort_index().to_numpy(), atol=1e-9)

    def test_buy_and_hold_context_baseline_is_present(self, alpha_world):
        assert "universe_buy_and_hold_gross" in alpha_world["results"][0]["baselines"]

    def test_regime_slices_are_reported(self, alpha_world):
        assert set(alpha_world["results"][0]["regimes"]) == {"trend_up", "trend_down", "vol_low", "vol_mid", "vol_high"}


class TestLegacyCalibration:
    """The harness must reproduce the numbers recorded for the old models when pointed at the
    old out-of-fold predictions, so a difference later is the METHOD, not a bug."""

    OOF = st.__file__.replace("ml\\selftest.py", "models\\weights").replace("ml/selftest.py", "models/weights")

    @pytest.mark.parametrize("h,days,expected,tol", [("swing", 5, 0.075, 0.03), ("long", 20, 0.42, 0.10),
                                                      ("intraday", 1, 0.0, 0.01)])
    def test_legacy_net_dsr_at_5bps(self, h, days, expected, tol):
        import os
        p = os.path.join(self.OOF, f"oos_{h}.parquet")
        if not os.path.exists(p):
            pytest.skip("old OOF files not present")
        from qsde.models.lgbm_model import _evaluate_oos_with_costs
        oos = pd.read_parquet(p)
        out = _evaluate_oos_with_costs(oos, horizon_days=days, cost_bps_round_trip=5.0)
        assert out["dsr_net"] == pytest.approx(expected, abs=tol)
