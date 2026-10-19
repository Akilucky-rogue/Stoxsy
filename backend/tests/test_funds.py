"""AMFI NAV parsing, fund classification and NAV returns (labelled cumulative vs annualised)."""
from datetime import date, datetime, timezone

import numpy as np
import pandas as pd
import pytest

from qsde.funds.classify import classify, split_heading
from qsde.funds.returns import risk_metrics, scheme_returns
from qsde.stream.adapters import amfi
from qsde.stream.base import RawItem

HISTORY = (
    "Scheme Code;NAV Name;Plan;Option;ISIN Div Payout/ISIN Growth;ISIN Div Reinvestment;Net Asset Value;Date\n\n"
    "Open Ended Schemes ( Money Market )\n\nTaurus Mutual Fund\n"
    "139619;Taurus Liquid - Growth;Direct Plan;Growth Option;INF846K01WO1;-;10.0000;01-Oct-2026\n"
    "139619;Taurus Liquid - Growth;Direct Plan;Growth Option;INF846K01WO1;-;10.0100;05-Oct-2026\n"
    "139620;Taurus Liquid - IDCW;Regular Plan;IDCW Option;INF846K01WP8;-;N.A.;05-Oct-2026\n\n"
    "Open Ended Schemes(Equity Scheme - Large Cap Fund)\n\nAxis Mutual Fund\n"
    "100001;Axis Bluechip Fund - Growth;Regular Plan;Growth Option;INF846K01111;INF846K01129;55.5000;05-Oct-2026\n"
)
NAVALL = (
    "Scheme Code;ISIN Div Payout/ ISIN Growth;ISIN Div Reinvestment;Scheme Name;Plan;Option;Net Asset Value;Date\n \n"
    "Open Ended Schemes(Debt Scheme - Banking and PSU Fund)\n \nAxis Mutual Fund\n \n"
    "135762;INF846K01WO1;-;Axis Banking & PSU Debt Fund;Direct Plan;Growth Option;29.3960;16-Oct-2026\n"
)


def test_history_parse_carries_category_and_amc_and_drops_unusable_navs():
    df = amfi.parse_amfi(HISTORY.encode())
    assert len(df) == 3 and df.attrs["dropped_rows"] == 1                                    # the N.A. NAV is dropped, not guessed
    t = df[df["scheme_code"] == 139619].iloc[0]
    assert t["amc"] == "Taurus Mutual Fund" and "Money Market" in t["category_raw"] and t["isin_a"] == "INF846K01WO1" and t["isin_b"] is None
    a = df[df["scheme_code"] == 100001].iloc[0]
    assert a["amc"] == "Axis Mutual Fund" and "Large Cap" in a["category_raw"] and a["nav"] == 55.5
    assert df["event_time"].dt.tz is not None


def test_navall_column_order_is_different_and_handled():
    df = amfi.parse_amfi(NAVALL.encode())
    r = df.iloc[0]
    assert (r["name"], r["plan"], r["option_type"], r["isin_a"], r["nav"]) == ("Axis Banking & PSU Debt Fund", "Direct Plan", "Growth Option", "INF846K01WO1", 29.396)
    with pytest.raises(ValueError):
        amfi.parse_amfi(b"A;B\n1;2\n")


def test_windows_are_contiguous_quarters_and_resumable():
    w = amfi.AmfiNavAdapter.quarter_windows(date(2016, 1, 1), date(2016, 12, 31))
    assert w[0] == (date(2016, 1, 1), date(2016, 3, 31)) and w[-1] == (date(2016, 10, 1), date(2016, 12, 31)) and len(w) == 4
    class F:
        calls = []
        def get(self, url, headers=None): self.calls.append(url); return HISTORY.encode()
    f = F()
    a = amfi.AmfiNavAdapter(f, windows=w, landed_windows={w[0], w[1]})
    got = a.fetch(None)
    assert [i.meta["from"] for i in got] == ["2016-07-01", "2016-10-01"] and len(f.calls) == 2


def test_classification_from_amfi_headings_and_names():
    assert split_heading("Open Ended Schemes(Debt Scheme - Banking and PSU Fund)") == ("Open Ended", "Debt Scheme", "Banking and PSU Fund")
    assert split_heading("Open Ended Schemes ( Money Market )") == ("Open Ended", "Money Market", None)
    c = classify("Open Ended Schemes(Equity Scheme - Large Cap Fund)", "Axis Bluechip Fund - Direct Plan - Growth", "Direct Plan", "Growth Option")
    assert (c["asset_class"], c["is_direct"], c["is_growth"], c["is_etf"]) == ("equity", True, True, False)
    assert classify("Open Ended Schemes(Debt Scheme - Liquid Fund)", "X Liquid Fund", "Regular Plan", "IDCW Option")["is_liquid"] is True
    g = classify("Other Scheme - Other ETFs", "Nippon India ETF Gold BeES", None, None)
    assert g["asset_class"] == "commodity" and g["is_etf"] is True
    i = classify("Open Ended Schemes(Other Scheme - FoF Overseas)", "Motilal Oswal Nasdaq 100 Fund of Fund", "Direct Plan", "Growth Option")
    assert i["asset_class"] == "international" and i["is_fof"] is True
    ix = classify("Open Ended Schemes(Other Scheme - Index Funds)", "UTI Nifty 50 Index Fund", "Regular Plan", "IDCW Option")
    assert ix["is_index"] is True and ix["is_growth"] is False
    assert classify("Open Ended Schemes ( Balanced )", "Old Balanced Fund")["asset_class"] == "hybrid"           # pre-2018 label
    assert classify(None, "Mystery")["asset_class"] == "other"


def nav_series(daily=0.0004, n=2000, start="2019-01-01"):
    idx = pd.bdate_range(start, periods=n)
    return pd.Series(10 * (1 + daily) ** np.arange(n), index=idx)


def test_returns_are_cumulative_for_short_horizons_and_annualised_only_from_one_year():
    s = nav_series()
    r = scheme_returns(s)
    assert set(r) == {"1D", "1W", "1M", "3M", "6M", "1Y", "3Y", "5Y"}
    for h in ("1D", "1W", "1M", "3M", "6M"):
        assert r[h]["annualised"] is None                                                         # never annualise a sub-year figure
    one_y = r["1Y"]
    assert one_y["annualised"] == pytest.approx((1 + one_y["cumulative"]) ** (365.25 / (pd.Timestamp(one_y["end"]) - pd.Timestamp(one_y["start"])).days) - 1)
    assert r["5Y"]["cumulative"] > r["3Y"]["cumulative"] > r["1Y"]["cumulative"] > 0
    # a constant daily rate: the annualised figure is about the same for 1Y, 3Y and 5Y
    assert r["3Y"]["annualised"] == pytest.approx(r["5Y"]["annualised"], rel=0.02)


def test_no_extrapolation_and_stale_start_navs_are_rejected():
    young = nav_series(n=300)                                                                      # ~14 months of history
    r = scheme_returns(young)
    assert "1Y" in r and "3Y" not in r and "5Y" not in r
    base = nav_series(n=400)
    gappy = base.drop(base.index[300:360])                                                         # a 60-business-day hole where the 3M start falls
    r = scheme_returns(gappy)
    assert "3M" not in r and "1D" in r                                                             # stale start NAV (> 7 days) is not used
    assert scheme_returns(pd.Series(dtype=float)) == {}


def test_risk_metrics_drawdown_and_volatility():
    s = nav_series(daily=0.0)
    s.iloc[1000:1100] *= 0.5                                                                      # a 50% fall that stays
    m = risk_metrics(s, window_days=2000)
    assert m["max_drawdown"] == pytest.approx(-0.5, abs=0.01) and m["ann_volatility"] > 0 and m["observations"] > 1000
    assert risk_metrics(nav_series(n=30)) == {}
