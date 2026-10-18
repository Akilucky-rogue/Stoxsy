"""Closed prediction loop: immutable ledger, outcome joiner (same labels as training), monitor, retrain trigger."""
import numpy as np
import pandas as pd
import pytest

from qsde.live_loop import ledger, monitor, outcomes
from qsde.ml.labels import make_labels
from qsde.ml.selftest import make_panel

PRICES, _FEATS, CAL = make_panel(n_sym=25, n_days=260, edge_bps=0.0, seed=11)


def preds(dates, model="m_v1", horizon="swing_5d", side=1, score_seed=0, sigma=None):
    rng = np.random.default_rng(score_seed)
    syms = sorted(PRICES["symbol"].unique())
    rows = []
    for d in dates:
        for s in syms:
            rows.append({"model_version": model, "symbol": s, "as_of_date": d, "horizon_id": horizon, "side": side,
                         "score": float(rng.normal()), "prediction_time": ledger.session_close_ist(d) + pd.Timedelta(hours=2),
                         "sigma": sigma})
    return pd.DataFrame(rows)


# ── ledger ───────────────────────────────────────────────────────────

class TestLedger:
    def test_ids_hashes_and_registry_label(self):
        p = ledger.prepare(preds(CAL[100:102]))
        assert p["prediction_id"].str.len().eq(32).all() and p["prediction_id"].is_unique
        assert p["label_id"].eq("tb_nextopen_v1").all()
        again = ledger.prepare(preds(CAL[100:102]))
        assert p["content_hash"].tolist() == again["content_hash"].tolist()                 # deterministic
        changed = preds(CAL[100:102])
        changed.loc[0, "score"] += 1.0
        assert ledger.prepare(changed)["content_hash"].iloc[0] != p["content_hash"].iloc[0]

    def test_a_prediction_stamped_before_the_session_close_is_a_pit_violation(self):
        p = preds(CAL[100:101])
        p["prediction_time"] = ledger.session_close_ist(CAL[100]) - pd.Timedelta(hours=1)
        with pytest.raises(ledger.PITViolation):
            ledger.prepare(p)

    def test_future_timestamps_and_bad_values_are_rejected(self):
        p = preds(CAL[100:101])
        with pytest.raises(ledger.PITViolation):
            ledger.prepare(p, now=ledger.session_close_ist(CAL[100]))                         # stamped 2h after 'now'
        bad = preds(CAL[100:101])
        bad.loc[0, "score"] = np.nan
        with pytest.raises(ValueError):
            ledger.prepare(bad)
        bad = preds(CAL[100:101]).assign(p_win=1.5)
        with pytest.raises(ValueError):
            ledger.prepare(bad)
        with pytest.raises(ValueError):
            ledger.prepare(preds(CAL[100:101]).drop(columns=["score"]))

    def test_relogging_identical_is_a_noop_and_changed_content_is_refused(self):
        p = ledger.prepare(preds(CAL[100:101]))
        existing = dict(zip(p["prediction_id"], p["content_hash"]))
        new, dup = ledger.split_by_existing(p, existing)
        assert len(new) == 0 and dup == len(p)
        changed = preds(CAL[100:101])
        changed["score"] += 0.5                                                              # same keys, different numbers
        with pytest.raises(ledger.ImmutableViolation):
            ledger.split_by_existing(ledger.prepare(changed), existing)
        newer = ledger.prepare(preds(CAL[100:101], model="m_v2"))                             # a changed model gets a new version
        assert len(ledger.split_by_existing(newer, existing)[0]) == len(newer)

    def test_duplicate_keys_in_one_batch_are_refused(self):
        p = preds(CAL[100:101])
        with pytest.raises(ledger.ImmutableViolation):
            ledger.prepare(pd.concat([p, p.iloc[:1]]))


# ── outcome joiner ───────────────────────────────────────────────────

class TestOutcomes:
    def test_outcomes_equal_the_training_labels(self):
        p = ledger.prepare(preds(CAL[120:130], sigma=None))
        p = p.assign(as_of_date=pd.to_datetime(p["as_of_date"]), horizon_id="swing_5d")
        oc = outcomes.resolve(p, PRICES)
        ev = p[["symbol", "as_of_date"]].rename(columns={"as_of_date": "date"})
        lab = make_labels(PRICES, ev, "swing_5d", side=1, drop_contaminated=False, keep_paths=False)
        j = outcomes.join_outcomes(p, oc).merge(lab, on=["symbol", "as_of_date"])
        assert len(j) == len(lab) > 0
        assert np.allclose(j["ret_net_x"], j["ret_net_y"]) and (j["status_x"] == j["status_y"]).all()

    def test_incomplete_windows_have_no_outcome(self):
        late = CAL[-3:]                                                                      # fewer than 5 sessions of future
        p = ledger.prepare(preds(late))
        p["as_of_date"] = pd.to_datetime(p["as_of_date"])
        oc = outcomes.resolve(p, PRICES)
        assert oc.empty
        assert len(outcomes.unresolved(p, oc)) == len(p)

    def test_data_through_cutoff_and_no_peeking_past_the_window(self):
        p = ledger.prepare(preds(CAL[200:204]))
        p["as_of_date"] = pd.to_datetime(p["as_of_date"])
        full = outcomes.resolve(p, PRICES).set_index("prediction_id")
        cut = CAL[204 + 5]                                                                   # window of the last prediction ends here
        part = outcomes.resolve(p, PRICES, data_through=cut).set_index("prediction_id")
        assert set(part.index) <= set(full.index) and len(part) > 0
        assert np.allclose(part["ret_net"], full.loc[part.index, "ret_net"])
        # bars AFTER the window cannot change an outcome
        px2 = PRICES.copy()
        px2.loc[px2["date"] > cut, ["open", "high", "low", "close"]] *= 3.0
        again = outcomes.resolve(p, px2).set_index("prediction_id")
        assert np.allclose(again.loc[part.index, "ret_net"], part["ret_net"])
        # and a later cut-off never resolves what the earlier one could not see
        early = outcomes.resolve(p, PRICES, data_through=CAL[203])
        assert early.empty

    def test_short_side_is_resolved_with_the_mirrored_barriers(self):
        pl = ledger.prepare(preds(CAL[150:152], side=1))
        ps = ledger.prepare(preds(CAL[150:152], side=-1, model="m_short"))
        for x in (pl, ps):
            x["as_of_date"] = pd.to_datetime(x["as_of_date"])
        ol, os_ = outcomes.resolve(pl, PRICES), outcomes.resolve(ps, PRICES)
        a = pl.merge(ol, on="prediction_id")[["symbol", "as_of_date", "entry_price", "stop_price", "target_price"]]
        b = ps.merge(os_, on="prediction_id")[["symbol", "as_of_date", "entry_price", "stop_price", "target_price"]]
        m = a.merge(b, on=["symbol", "as_of_date"])
        assert (m["stop_price_x"] < m["entry_price_x"]).all() and (m["stop_price_y"] > m["entry_price_y"]).all()


# ── monitor ──────────────────────────────────────────────────────────

class TestMonitor:
    def test_brier_logloss_and_calibration_slope(self):
        rng = np.random.default_rng(0)
        p = rng.uniform(0.05, 0.95, 20000)
        y = (rng.uniform(size=p.size) < p).astype(float)
        slope, icpt = monitor.calibration_slope_intercept(p, y)
        assert abs(slope - 1.0) < 0.08 and abs(icpt) < 0.06                                  # calibrated
        over = np.clip(0.5 + 2.0 * (p - 0.5), 0.01, 0.99)                                    # over-confident version
        assert monitor.calibration_slope_intercept(over, y)[0] < 0.7
        assert monitor.brier(np.array([1.0, 0.0]), np.array([1.0, 0.0])) == 0.0
        assert monitor.log_loss(p, y) < monitor.log_loss(np.full_like(p, 0.5), y)

    def test_psi_zero_for_same_distribution_and_large_for_a_shift(self):
        rng = np.random.default_rng(1)
        a, b = rng.normal(size=5000), rng.normal(size=5000)
        assert monitor.psi(a, b) < 0.05
        assert monitor.psi(a, rng.normal(1.5, 1.0, 5000)) > 0.5
        assert np.isnan(monitor.psi(a[:10], b[:10]))

    def _joined(self, informative: bool, n_dates=80, n=30, seed=2):
        rng = np.random.default_rng(seed)
        rows = []
        for i in range(n_dates):
            s = rng.normal(size=n)
            r = (0.01 * s if informative else 0.0) + rng.normal(0, 0.03, n)
            rows.append(pd.DataFrame({"as_of_date": pd.Timestamp("2021-01-04") + pd.Timedelta(days=i), "score": s, "ret_net": r,
                                      "p_win": 1 / (1 + np.exp(-3 * s * (0.3 if informative else 0))), "excluded": False}))
        return pd.concat(rows, ignore_index=True)

    def test_ranking_and_economics_separate_signal_from_noise(self):
        good, bad = monitor.health_report(self._joined(True)), monitor.health_report(self._joined(False))
        assert good["ranking"]["ic_mean"] > 0.15 and abs(bad["ranking"]["ic_mean"]) < 0.06
        assert good["economics"]["excess_mean"] > 0.003 and good["ranking"]["lift"] > 1.05
        assert good["n_resolved"] == 80 * 30

    def test_excluded_rows_do_not_count(self):
        j = self._joined(True)
        j.loc[j.index[:500], "excluded"] = True
        assert monitor.health_report(j)["n_resolved"] == len(j) - 500

    def test_retrain_decision_never_trusts_thin_live_evidence_but_still_schedules(self):
        thin = {"n_resolved": 40, "n_dates": 5, "drift": {"n_above": 50}}
        d = monitor.retrain_decision(thin, "swing_5d", pd.Timestamp("2026-10-19"), last_retrain=pd.Timestamp("2026-10-15"))
        assert not d["trigger"] and d["suppressed"] and "thin" not in d["action"]
        due = monitor.retrain_decision(thin, "swing_5d", pd.Timestamp("2026-10-19"), last_retrain=pd.Timestamp("2026-08-01"))
        assert due["trigger"] and due["kinds"] == ["scheduled"]

    def test_retrain_decision_triggers_on_drift_calibration_and_data(self):
        rep = {"n_resolved": 900, "n_dates": 120, "drift": {"n_above": 9},
               "calibration": {"slope": 0.4}, "ranking": {"ic_rolling": 0.001, "reference_ic": 0.05},
               "economics": {"n_dates": 100, "excess_mean": -0.002}}
        d = monitor.retrain_decision(rep, "swing_5d", pd.Timestamp("2026-10-19"), last_retrain=pd.Timestamp("2026-10-18"),
                                     new_confirmed_ca=3)
        assert d["trigger"] and set(d["kinds"]) == {"drift", "data"} and len(d["reasons"]) == 5
        assert "owner approval" in d["action"]

    def test_kill_switch(self):
        ok = {"calibration": {"slope": 0.9}, "economics": {"n_dates": 100, "max_drawdown_cum": -0.1}, "drift": {"max_psi": 0.2}}
        assert monitor.kill_switch(ok) == []
        bad = {"calibration": {"slope": 0.1}, "economics": {"n_dates": 100, "max_drawdown_cum": -0.9}, "drift": {"max_psi": 3.0}}
        assert len(monitor.kill_switch(bad, data_quality_ok=False)) == 4


# ── database (rolled back; the table is append-only so nothing is left behind) ──

def test_prediction_log_rejects_update_and_delete():
    psycopg2 = pytest.importorskip("psycopg2")
    from qsde.db.connection import get_sync_conn
    p = ledger.prepare(preds(CAL[100:101]).head(1)).iloc[0]
    try:
        with get_sync_conn() as conn:
            cur = conn.cursor()
            cur.execute("""INSERT INTO prediction_log (prediction_id, model_version, symbol, as_of_date, prediction_time, known_at,
                           horizon_id, label_id, side, score, content_hash) VALUES (%s,'t','S000','2020-01-01',now(),now(),
                           'swing_5d','tb_nextopen_v1',1,0.1,%s)""", (p["prediction_id"], p["content_hash"]))
            for stmt in ("UPDATE prediction_log SET score = 9", "DELETE FROM prediction_log"):
                cur.execute("SAVEPOINT s")
                with pytest.raises(psycopg2.Error) as e:
                    cur.execute(stmt)
                assert "append-only" in str(e.value)
                cur.execute("ROLLBACK TO SAVEPOINT s")
            conn.rollback()
            raise RuntimeError("rolled back")                                                 # never commit the probe row
    except RuntimeError:
        pass
    except psycopg2.OperationalError:
        pytest.skip("database not reachable")
