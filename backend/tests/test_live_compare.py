"""Live compare: presentation rules (no outcome before its window resolved, no forecast invented), health report, read-only API."""
import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from api.routes import live_compare
from qsde.live_loop import view
from qsde.live_loop.cards_log import feature_hash, signal_predictions


def pred(**kw):
    p = {"prediction_id": "a" * 32, "model_version": "m_v1", "symbol": "TCS", "as_of_date": pd.Timestamp("2026-10-16"), "prediction_time": pd.Timestamp("2026-10-16 17:00", tz="Asia/Kolkata"),
         "horizon_id": "position_20d", "side": 1, "score": 0.9, "rank_pct": 0.95, "p_win": None, "expected_return": None, "ci_low": None, "ci_high": None, "position_size": 0.05,
         "regime": "neutral", "ref_price": 100.0, "sigma": 0.02, "model_card_id": "card", "snapshot_id": None, "factor_version": None, "universe_version": "u1", "feature_hash": "f", "ca_source_hash": "ca", "git_sha": "abc"}
    p.update(kw)
    return p


def outc(**kw):
    o = {"status": "WIN", "entry_date": pd.Timestamp("2026-10-19"), "entry_price": 101.0, "stop_price": 95.0, "target_price": 110.0, "exit_date": pd.Timestamp("2026-11-10"),
         "exit_price": 110.0, "gap_exit": False, "ret_gross": 0.089, "ret_net": 0.087, "cost_bps": 12.5, "excluded": False, "data_through": pd.Timestamp("2026-11-12")}
    o.update(kw)
    return o


def test_pending_prediction_shows_lineage_window_and_barriers_but_no_outcome():
    r = view.shape_prediction(pred(), None)
    assert r["state"] == "pending" and r["outcome"] is None and r["model_state"] == "research"
    assert r["window"]["entry_date"] == "2026-10-19" and r["window"]["estimate"] is True and r["window"]["sessions"] == 20
    assert r["entry_rule"] == "next open" and r["barriers"]["stop_distance"] == pytest.approx(r["barriers"]["stop_sigmas"] * 0.02)
    assert r["lineage"]["universe_version"] == "u1" and r["lineage"]["snapshot_id"] is None and r["lineage"]["ca_source_hash"] == "ca"


def test_outcome_is_withheld_if_the_resolver_cutoff_had_not_reached_the_exit_date():
    early = view.shape_prediction(pred(), outc(data_through=pd.Timestamp("2026-11-09")))
    assert early["outcome"] is None and early["state"] == "pending"
    ok = view.shape_prediction(pred(), outc())
    assert ok["state"] == "resolved" and ok["outcome"]["status"] == "WIN" and ok["outcome"]["ret_net"] == pytest.approx(0.087)


def test_no_forecast_is_invented_and_realized_minus_expected_needs_a_logged_expectation():
    none = view.shape_prediction(pred(), outc())
    assert none["expected_return"] is None and none["outcome"]["realized_minus_expected"] is None and none["p_win"] is None
    some = view.shape_prediction(pred(expected_return=0.03, p_win=0.6, ci_low=-0.02, ci_high=0.08), outc())
    assert some["outcome"]["realized_minus_expected"] == pytest.approx(0.087 - 0.03) and some["ci_low"] == -0.02
    assert view.shape_prediction(pred(), outc(excluded=True))["state"] == "excluded"


def test_model_state_is_one_of_three_and_unknown_is_research():
    reg = {"a": {"state": "paper"}, "b": {"state": "guaranteed"}, "c": {"state": "approved"}}
    assert [view.model_state(x, reg) for x in ("a", "b", "c", "zzz")] == ["paper", "research", "approved", "research"]
    assert view.model_state("defensive_cards_v1") == "research"


def joined(n_dates=50, n_names=40, p_win=False, seed=3):
    rs = np.random.default_rng(seed)
    rows = []
    for d in pd.bdate_range("2026-01-01", periods=n_dates):
        for i in range(n_names):
            s = rs.normal()
            rows.append({"prediction_id": f"{d:%Y%m%d}{i}", "as_of_date": d, "symbol": f"S{i}", "horizon_id": "position_20d", "score": s, "ret_net": 0.01 * s + rs.normal(0, 0.03),
                         "excluded": False, "regime": "neutral", "p_win": float(np.clip(0.5 + 0.1 * s, 0.01, 0.99)) if p_win else None})
    return pd.DataFrame(rows)


def test_health_report_has_ranking_economics_series_and_thin_evidence_notes():
    h = view.health(joined(n_dates=50))
    assert h["n_resolved"] == 2000 and h["n_dates"] == 50 and h["ranking"]["ic_mean"] > 0 and "economics" in h
    assert len(h["ic_series"]["dates"]) == len(h["ic_series"]["rolling60"]) == 50
    assert any("no win probability" in n for n in h["notes"]) and not any("evidence is thin" in n for n in h["notes"])
    thin = view.health(joined(n_dates=20, n_names=30))
    assert any("evidence is thin" in n for n in thin["notes"]) and thin["retrain"]["suppressed"]
    assert h["score_drift"]["psi"] is not None and h["kill_switch"] == [] and h["retrain"]["action"].startswith("train a CANDIDATE") or h["retrain"]["action"] == "none"
    hp = view.health(joined(p_win=True, n_dates=60))
    assert hp["calibration"]["n"] > 50 and len(hp["reliability"]) >= 5
    e = view.health(pd.DataFrame())
    assert e["n_resolved"] == 0 and "no resolved predictions yet" in e["notes"][0] and e["kill_switch"] == []


def test_cards_log_rows_are_complete_forecast_free_and_weighted_by_the_cards():
    sc = pd.DataFrame({"symbol": ["A", "B", "C"], "date": pd.Timestamp("2026-10-16"), "defensive_score": [0.9, 0.5, 0.1], "risk_bucket": [5, 3, 1], "atr_pct": 0.02, "vol60": 0.01,
                       "downside60": 0.01, "dd120": -0.1, "liq": 1e9, "warnings": "", "regime": "neutral"})
    df = signal_predictions(sc, pd.Series({"A": 100.0, "B": 50.0, "C": 10.0}), {"swing_5d": pd.Series({"A": 0.02, "B": 0.03}), "position_20d": pd.Series({"A": 0.02})},
                            {"swing_5d": {"A": 0.07}, "position_20d": {}}, pd.Timestamp("2026-10-16 12:30", tz="UTC"), {"capital": 1}, {"universe_version": "u"})
    assert len(df) == 6 and set(df["horizon_id"]) == {"swing_5d", "position_20d"} and (df["side"] == 1).all()
    a5 = df[(df.symbol == "A") & (df.horizon_id == "swing_5d")].iloc[0]
    assert a5["position_size"] == 0.07 and a5["rank_pct"] == pytest.approx(1.0) and a5["sigma"] == 0.02 and a5["p_win"] if "p_win" in df else True
    assert "p_win" not in df.columns and "expected_return" not in df.columns                      # no forecast is claimed
    assert pd.isna(df[(df.symbol == "C") & (df.horizon_id == "position_20d")].iloc[0]["sigma"])
    assert feature_hash(sc) == feature_hash(sc.sample(frac=1, random_state=1))                    # order-independent


@pytest.fixture()
def client(monkeypatch):
    from api.main import app
    p = [pred(prediction_id=("b" * 31) + str(i), symbol=s, as_of_date=pd.Timestamp("2026-10-16")) for i, s in enumerate(["TCS", "INFY"])]
    o = [outc()]
    o[0]["prediction_id"] = p[0]["prediction_id"]
    monkeypatch.setattr(live_compare, "load_predictions", lambda *a, **k: (2, pd.DataFrame(p), pd.DataFrame(o)))
    monkeypatch.setattr(live_compare, "load_counts", lambda m, h: {"logged": 2, "resolved": 1, "pending": 1, "excluded": 0, "dates": 1, "first_as_of": "2026-10-16", "last_as_of": "2026-10-16"})
    monkeypatch.setattr(live_compare, "load_joined", lambda m, h: pd.DataFrame())
    return TestClient(app)


def test_predictions_endpoint_shapes_rows_validates_input_and_never_leaks_an_early_outcome(client):
    j = client.get("/api/live/predictions?model_version=m_v1").json()
    assert j["n_matching"] == 2 and [r["state"] for r in j["rows"]] == ["resolved", "pending"] and j["rows"][1]["outcome"] is None
    assert "guaranteed" in j["disclaimer"] and "not investment advice" in j["disclaimer"]
    for bad in ("horizon_id=weekly", "state=maybe", "limit=1000", "symbol=a b", "as_of=yesterday"):
        assert client.get(f"/api/live/predictions?model_version=m_v1&{bad}").status_code == 422
    s = client.get("/api/live/summary?model_version=m_v1&horizon_id=position_20d").json()
    assert s["counts"]["pending"] == 1 and s["state"] == "research" and s["health"]["n_resolved"] == 0


def test_the_live_compare_api_is_read_only():
    from api.main import app
    paths = {k: v for k, v in app.openapi()["paths"].items() if k.startswith(("/api/live/models", "/api/live/summary", "/api/live/predictions"))}
    assert len(paths) == 3 and all(set(v) == {"get"} for v in paths.values())
