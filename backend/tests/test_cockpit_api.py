"""Read-only cockpit endpoints: typed responses, path-traversal safety, the signal file contract."""
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from api.routes import cockpit


def tmp_path_of(client):
    return client.tmp


@pytest.fixture()
def client(monkeypatch, tmp_path):
    from api.main import app
    monkeypatch.setattr(cockpit, "REPORT_DIRS", {"research": tmp_path / "research", "model_cards": tmp_path / "cards"})
    (tmp_path / "research").mkdir()
    (tmp_path / "cards").mkdir()
    (tmp_path / "research" / "a.md").write_text("# Title A\n\nbody", encoding="utf-8")
    (tmp_path / "secret.md").write_text("# not a report", encoding="utf-8")
    sig = pd.DataFrame({"date": pd.Timestamp("2026-10-16"), "symbol": list("ABCDEF"), "defensive_score": [0.9, 0.8, 0.6, 0.4, 0.2, 0.1],
                        "risk_bucket": [5, 5, 3, 2, 1, 1], "atr_pct": 0.02, "vol60": 0.01, "downside60": 0.01, "dd120": -0.1, "liq": 1e9,
                        "warnings": ["", "", "", "", "high-risk quintile", "high-risk quintile"], "regime": "neutral"})
    sig.to_parquet(tmp_path / "latest.parquet")
    monkeypatch.setattr(cockpit, "SIGNAL", tmp_path / "latest.parquet")
    monkeypatch.setattr(cockpit, "DEF_EVAL", tmp_path / "missing.json")
    c = TestClient(app)
    c.tmp = tmp_path
    return c


def test_defensive_latest_filters_sorts_and_reports_bucket_counts(client):
    r = client.get("/api/defensive/latest?limit=3").json()
    assert r["as_of"] == "2026-10-16" and r["regime"] == "neutral" and r["n"] == 6
    assert [x["symbol"] for x in r["rows"]] == ["A", "B", "C"] and r["bucket_counts"] == {"1": 2, "2": 1, "3": 1, "5": 2}
    assert "not investment advice" in r["disclaimer"] and r["evaluation"] is None
    assert r["bucket_profile"]["1"]["n"] == 2 and r["bucket_profile"]["5"]["median_atr_pct"] == pytest.approx(0.02)
    assert [x["symbol"] for x in client.get("/api/defensive/latest?bucket=1&sort=symbol").json()["rows"]] == ["E", "F"]
    assert client.get("/api/defensive/latest?q=c").json()["n"] == 1
    assert client.get("/api/defensive/latest?bucket=9").status_code == 422


def test_reports_list_and_read_and_reject_traversal(client):
    items = client.get("/api/research/reports").json()
    assert [(i["group"], i["name"], i["title"], i["superseded"]) for i in items] == [("research", "a.md", "Title A", False)]
    nl = chr(10)
    (tmp_path_of(client) / "research" / "SUPERSEDED_b.md").write_text("> **SUPERSEDED: do not use**" + nl + nl + "# Real title B" + nl, encoding="utf-8")
    by = {i["name"]: i for i in client.get("/api/research/reports").json()}
    assert by["SUPERSEDED_b.md"]["superseded"] is True and by["SUPERSEDED_b.md"]["title"] == "Real title B"
    assert client.get("/api/research/reports/research/a.md").json()["markdown"].startswith("# Title A")
    for bad in ("../secret.md", "..%2Fsecret.md", "a.txt", "..\secret.md"):
        assert client.get(f"/api/research/reports/research/{bad}").status_code in (404, 422)
    assert client.get("/api/research/reports/unknown/a.md").status_code == 404


def test_system_status_shape(client):
    r = client.get("/api/system/status")
    assert r.status_code == 200
    j = r.json()
    assert {"database", "ohlcv_latest", "auth_enforced", "promotion_enabled", "current_snapshots", "adjustment_series"} <= set(j)
    assert j["promotion_enabled"] is False and j["scheduled_downloads_enabled"] is False


def test_decision_cards_endpoint_validates_parameters_and_returns_a_typed_book(client, monkeypatch):
    import numpy as np
    from qsde.ml import data as mldata
    dates = pd.bdate_range(end=pd.Timestamp("2026-10-16"), periods=300)
    rng = np.random.default_rng(1)
    rows = [pd.DataFrame({"symbol": s, "date": dates, "close": 100 * np.exp(np.cumsum(rng.normal(0, 0.01, len(dates))))}) for s in "ABCDEF"]
    monkeypatch.setattr(mldata, "load_prices", lambda symbols=None, policy="official", start=None: pd.concat(rows, ignore_index=True))
    sig = pd.read_parquet(cockpit.SIGNAL).assign(risk_bucket=5, liq=5e9)
    sig.to_parquet(cockpit.SIGNAL)
    r = client.get("/api/decision/cards?capital=1000000&risk_per_position=0.005&max_positions=3&horizon=swing_5d")
    assert r.status_code == 200
    j = r.json()
    assert j["summary"]["evidence"] == "RESEARCH_ONLY" and j["summary"]["horizon_sessions"] == 5 and 1 <= len(j["cards"]) <= 3
    assert all(c["quantity"] >= 1 and c["stop"] < c["last_close"] and c["evidence"] == "RESEARCH_ONLY" for c in j["cards"])
    assert "not investment advice" in j["disclaimer"]
    for bad in ("capital=10", "risk_per_position=0.5", "horizon=intraday_1d", "min_bucket=1", "max_positions=999"):
        assert client.get(f"/api/decision/cards?{bad}").status_code == 422


@pytest.mark.real_data
def test_funds_endpoint_labels_basis_statuses_and_validates_input(client):
    r = client.get("/api/funds/schemes?limit=5&option=growth")
    assert r.status_code == 200
    j = r.json()
    assert j["n_matching"] >= 1 and 1 <= len(j["rows"]) <= 5 and "not a forecast" in j["disclaimer"] and "not investment advice" in j["disclaimer"]
    for row in j["rows"]:
        assert row["basis"] == "total_return_after_expenses" and row["is_growth"] is True and row["quality"]["level"] in ("ok", "caution", "poor")
        for h, x in row["returns"].items():
            assert x["status"] in ("ok", "insufficient_history", "stale_start", "nav_discontinuity")
            if x["status"] == "ok":
                assert (x["annualised"] is None) or h in ("1Y", "3Y", "5Y")             # never annualise below one year
            else:
                assert x["detail"] and "cumulative" not in x                              # a missing figure says why and carries no number
    for bad in ("asset_class=bogus", "plan=other", "limit=1000", "option=maybe", "sort=drop", "quality=great"):
        assert client.get(f"/api/funds/schemes?{bad}").status_code == 422
    idcw = client.get("/api/funds/schemes?limit=5&option=idcw").json()
    assert all(x["basis"] == "price_return_only" for x in idcw["rows"])
    etf = client.get("/api/funds/schemes?limit=5&option=etf").json()
    assert all(x["basis"] in ("price_return", "total_return_after_expenses", "price_return_only") and x["is_etf"] for x in etf["rows"])
    srt = client.get("/api/funds/schemes?limit=10&sort=ret_1y&order=desc&asset_class=equity&plan=direct").json()["rows"]
    vals = [x["returns"].get("1Y", {}).get("annualised") for x in srt if x["returns"].get("1Y", {}).get("status") == "ok"]
    assert vals == sorted(vals, reverse=True)


@pytest.mark.real_data
def test_funds_facets_and_detail_series_are_rebased_and_flag_events(client):
    f = client.get("/api/funds/facets").json()
    assert f["asset_classes"] and f["amcs"] and f["fund_groups"] and f["categories"]
    code = client.get("/api/funds/schemes?limit=1&option=growth&asset_class=equity&plan=direct&sort=ret_3y&order=desc").json()["rows"][0]["scheme_code"]
    d = client.get(f"/api/funds/schemes/{code}?window=3y").json()
    assert d["fund"]["scheme_code"] == code and d["series"]["value"][0] == 100.0 and len(d["series"]["dates"]) == len(d["series"]["drawdown"]) <= 520
    assert max(d["series"]["drawdown"]) <= 0 and "unit_changes" in d["events"] and "gaps_over_30d" in d["events"]
    assert client.get("/api/funds/schemes/1").status_code in (404, 200)
    assert client.get("/api/funds/schemes/999999999").status_code == 404
