"""Portfolio cockpit: holdings parsing (formats, rejections, aggregation) and the pure analytics."""
import numpy as np
import pandas as pd
import pytest

from qsde.portfolio import analytics
from qsde.portfolio.holdings import parse_holdings

KNOWN = {"TCS", "INFY", "HDFCBANK", "RELIANCE", "NEWNAME"}
NL = chr(10)


def test_parses_kite_layout_with_suffixes_commas_and_totals():
    csv_text = NL.join(["Instrument,Qty.,Avg. cost,LTP,Cur. val,P&L", "TCS,10,\"3,200.50\",3300,33000,990", "INFY-BE,5,1500,1600,8000,500", "Total,,,,41000,1490"])
    r = parse_holdings(csv_text, KNOWN)
    assert [(h["symbol"], h["quantity"], h["avg_cost"]) for h in r.holdings] == [("INFY", 5.0, 1500.0), ("TCS", 10.0, 3200.5)]
    assert r.n_rejected == 0 and len(r.sha256) == 64


def test_rejections_are_reported_with_reasons_and_not_silent():
    csv_text = NL.join(["symbol,quantity,avg_cost", "TCS,10,3000", "SHORTY,-5,100", "GHOSTCO,10,50", "HDFCBANK,abc,1000", "RELIANCE,3,-1"])
    r = parse_holdings(csv_text, KNOWN)
    assert [h["symbol"] for h in r.holdings] == ["TCS"]
    why = {i["symbol"]: i["reason"] for i in r.issues if i["severity"] == "rejected"}
    assert "not positive" in why["SHORTY"] and "no NSE price history" in why["GHOSTCO"] and "not a number" in why["HDFCBANK"] and "negative" in why["RELIANCE"]


def test_duplicates_aggregate_with_weighted_cost_and_rename_and_isin_resolution():
    csv_text = NL.join(["symbol,isin,quantity,avg_cost", "TCS,INE467B01029,10,3000", "TCS,INE467B01029,30,3400", "OLDNAME,INE000X01010,2,50", "ZZZ,INE111A01011,4,10"])
    r = parse_holdings(csv_text, KNOWN, isin_to_symbol={"INE111A01011": "INFY"}, renamed={"OLDNAME": "NEWNAME"})
    h = {x["symbol"]: x for x in r.holdings}
    assert h["TCS"]["quantity"] == 40 and h["TCS"]["avg_cost"] == pytest.approx(3300.0)
    assert "NEWNAME" in h and "INFY" in h
    assert any("duplicate" in i["reason"] for i in r.issues) and any("renamed" in i["reason"] for i in r.issues) and any("ISIN" in i["reason"] for i in r.issues)


def test_missing_columns_and_empty_files_fail_loudly():
    with pytest.raises(ValueError, match="average cost"):
        parse_holdings(NL.join(["symbol,quantity", "TCS,1"]), KNOWN)
    with pytest.raises(ValueError, match="no data rows"):
        parse_holdings("symbol,quantity,avg_cost", KNOWN)
    with pytest.raises(ValueError, match="no valid holdings"):
        parse_holdings(NL.join(["symbol,quantity,avg_cost", "NOPE,1,1"]), KNOWN)


def test_zerodha_console_quantity_available_warns_about_pledged():
    csv_text = NL.join(["Symbol,ISIN,Quantity Available,Average Price", "TCS,INE467B01029,10,3000"])
    r = parse_holdings(csv_text, KNOWN)
    assert r.holdings[0]["quantity"] == 10 and any("pledged" in i["reason"] for i in r.issues)


def test_valuation_pnl_weights_and_stale_flag():
    h = pd.DataFrame({"symbol": ["A", "B", "C"], "quantity": [10.0, 20.0, 5.0], "avg_cost": [100.0, 50.0, 10.0]})
    last = pd.DataFrame({"symbol": ["A", "B"], "date": pd.to_datetime(["2026-10-16", "2026-10-01"]), "close": [120.0, 40.0]})
    df = analytics.value_holdings(h, last, pd.Timestamp("2026-10-16"))
    a, b, c = (df.set_index("symbol").loc[s] for s in "ABC")
    assert a["value"] == 1200 and a["pnl"] == 200 and a["pnl_pct"] == pytest.approx(0.2)
    assert b["pnl"] == -200 and bool(b["stale"]) and not bool(a["stale"]) and np.isnan(c["value"])
    assert df["weight"].sum(skipna=True) == pytest.approx(1.0) and analytics.summarise(df)["n_unpriced"] == 1


def test_concentration_and_limit_flags():
    w = pd.Series({"A": 0.5, "B": 0.25, "C": 0.15, "D": 0.10})
    c = analytics.concentration(w)
    assert c["top1"] == 0.5 and c["top3"] == pytest.approx(0.9) and c["hhi"] == pytest.approx(0.25 + 0.0625 + 0.0225 + 0.01)
    assert c["effective_n"] == pytest.approx(1 / c["hhi"]) and [x["symbol"] for x in c["above_limit"]] == ["A", "B", "C", "D"]


def test_history_is_proforma_with_drawdown_and_benchmark():
    idx = pd.bdate_range("2025-01-01", periods=300)
    up = pd.Series(np.linspace(100, 200, 300), index=idx)
    dip = up.copy()
    dip.iloc[150:200] = dip.iloc[150:200] * 0.7
    closes = pd.DataFrame({"A": up, "B": dip})
    h = pd.DataFrame({"symbol": ["A", "B", "Z"], "quantity": [1.0, 1.0, 5.0]})
    r = analytics.history(h, closes, bench=up, lookback_days=500)
    assert r["names_missing"] == ["Z"] and r["max_drawdown"] < -0.1 and r["current_drawdown"] <= 0
    assert r["benchmark"]["total_return"] == pytest.approx(1.0, abs=0.05) and r["benchmark"]["max_drawdown"] == pytest.approx(0.0, abs=1e-9)
    assert len(r["series"]["value"]) == len(r["series"]["drawdown"]) == r["sessions"]
    assert analytics.history(h.iloc[[2]], closes, None) is None


def test_defensive_profile_shares_sum_to_one_with_not_scored_bucket():
    df = pd.DataFrame({"symbol": ["A", "B", "C"], "value": [50.0, 30.0, 20.0]})
    sc = pd.DataFrame({"symbol": ["A", "B"], "risk_bucket": [5, 2], "defensive_score": [.9, .2]})
    p = analytics.defensive_profile(df, sc)
    assert p["scored_share"] == pytest.approx(0.8) and p["buckets"]["not scored"]["share"] == pytest.approx(0.2)
    assert sum(b["share"] for b in p["buckets"].values()) == pytest.approx(1.0)
    assert analytics.defensive_profile(df, None)["available"] is False
