"""BSE corporate actions: export parsing, action classification, evidence-only reconciliation."""
import numpy as np
import pandas as pd
import pytest

from qsde.ingestion import bse_corporate_actions as B

NL = chr(10)
CSV = NL.join([
    "Security Code,Security Name,Ex Date,Purpose,Record Date,BC Start Date,BC End Date,ND Start Date,ND End Date,Actual Payment Date",
    "500001,ALPHA LTD,15 Mar 2024,Bonus issue 1:1,16 Mar 2024,,,,,",
    "500002,BETA LTD,15 Mar 2024,Stock Split From Rs.10/- To Rs.2/-,16 Mar 2024,,,,,",
    "500003,GAMMA LTD,18 Mar 2024,Dividend - Rs. - 5.0000,19 Mar 2024,,,,,25 Mar 2024",
    "500004,DELTA LTD,18 Mar 2024,Rights issue of Equity Shares in the ratio of 1:5 @ Premium Rs.40/-,19 Mar 2024,,,,,",
    "500005,EPS LTD,20 Mar 2024,Demerger,21 Mar 2024,,,,,",
    "500006,ZETA LTD,not a date,Bonus issue 1:2,,,,,,",
    "ABC,ETA LTD,15 Mar 2024,Bonus issue 1:2,,,,,,",
    "500008,THETA LTD,15 Mar 2024,,,,,,,",
    "500009,IOTA LTD,22 Mar 2024,Buy Back of Shares,,,,,,",
])


def test_export_is_parsed_and_every_bad_row_is_reported_not_dropped():
    ex = B.read_export(CSV.encode(), "ca.csv")
    assert ex.n_rows == 9 and len(ex.frame) == 6 and ex.n_bad == 3 and len(ex.sha256) == 64
    why = " | ".join(i["reason"] for i in ex.issues)
    assert "unreadable" in why and "not a BSE scrip code" in why and "no purpose text" in why
    assert ex.frame["ex_date"].iloc[0] == pd.Timestamp("2024-03-15") and ex.frame["payment_date"].iloc[2] == pd.Timestamp("2024-03-25")


def test_missing_required_columns_and_empty_files_fail_loudly():
    with pytest.raises(ValueError, match="could not find column"):
        B.read_export(NL.join(["Name,Date", "x,y"]).encode())
    with pytest.raises(ValueError, match="no valid rows"):
        B.read_export(NL.join(["Security Code,Ex Date,Purpose", "12,bad,"]).encode())
    with pytest.raises(ValueError, match="Excel|openpyxl"):
        B.read_export(b"PK\x03\x04 not really excel", "x.xlsx")


def test_purpose_text_is_classified_and_factors_follow_the_nse_convention():
    c = B.classify_purpose
    assert (c("Bonus issue 1:1")["action_type"], round(c("Bonus issue 1:1")["factor"], 6)) == ("bonus", 0.5)
    assert round(c("Bonus issue 1:10")["factor"], 6) == round(10 / 11, 6)
    s = c("Stock  Split From Rs.10/- To Rs.2/-")
    assert (s["action_type"], round(s["factor"], 6)) == ("split", 0.2)
    assert c("Dividend - Rs. - 5.0000")["amount"] == 5.0 and c("Dividend - Rs. - 5.0000")["action_type"] == "dividend"
    r = c("Rights issue of Equity Shares in the ratio of 1:5 @ Premium Rs.40/-")
    assert r["action_type"] == "rights" and r["ratio_text"] == "1:5" and r["factor"] is None
    assert c("Scheme of Arrangement / Demerger")["action_type"] == "demerger_or_scheme"
    assert c("Buy Back of Shares")["action_type"] == "buyback" and c("Annual General Meeting")["action_type"] == "administrative"
    assert B.content_hash({"scrip_code": 1, "ex_date": "2024-03-15", "purpose": "Bonus  issue 1:1"}) == B.content_hash({"scrip_code": 1, "ex_date": pd.Timestamp("2024-03-15"), "purpose": "bonus issue 1:1"})


def bars(code, ex, factor=None, n_before=25, n_after=8, base=100.0, gap=True):
    d = pd.bdate_range(end=pd.Timestamp(ex) - pd.Timedelta(days=1), periods=n_before)
    d2 = pd.bdate_range(start=pd.Timestamp(ex), periods=n_after)
    rs = np.random.default_rng(code)
    p1 = base * (1 + rs.normal(0, 0.003, n_before)).cumprod()
    p2 = (base * (factor if (gap and factor) else 1.0)) * (1 + rs.normal(0, 0.003, n_after)).cumprod()
    out = pd.DataFrame({"scrip_code": code, "date": list(d) + list(d2), "open": np.r_[p1, p2], "close": np.r_[p1, p2]})
    return out


def test_reconcile_confirms_rejects_and_defers_without_adjusting_anything():
    ex = B.read_export(CSV.encode())
    acts = B.enrich(ex.frame, {500001: "INE000A01010"}).reset_index(drop=True)
    acts.insert(0, "id", range(1, len(acts) + 1))
    px = pd.concat([bars(500001, "2024-03-15", 0.5), bars(500002, "2024-03-15", 1.0, gap=False), bars(500003, "2024-03-18", 1.0, gap=False)])
    res = B.reconcile(acts, px, calendar=pd.bdate_range("2024-01-01", "2024-06-30")).set_index("id")
    by = {int(r.scrip_code): int(r.id) for r in acts.itertuples()}
    assert res.loc[by[500001], "classification"] == "confirmed" and abs(res.loc[by[500001], "factor_error"]) < 0.05
    assert res.loc[by[500002], "classification"] == "rejected"                                   # a 1:5 split listed, but no gap in the prices
    assert res.loc[by[500003], "classification"] == "confirmed" and "of the last close" in res.loc[by[500003], "reason"]
    for code in (500004, 500005, 500009):
        assert res.loc[by[code], "classification"] == "needs_review"
    assert set(res["classification"]) <= {"confirmed", "needs_review", "rejected"}
    assert acts["isin"].iloc[0] == "INE000A01010" and acts["isin"].iloc[1] is None


def test_actions_without_price_bars_need_review_and_a_huge_dividend_does_not_pass():
    acts = pd.DataFrame({"id": [1, 2, 3], "scrip_code": [500010, 500011, 500012], "ex_date": pd.to_datetime(["2024-03-15"] * 3), "purpose": ["Bonus issue 1:1", "Dividend - Rs. - 80.0", "Dividend"],
                         "action_type": ["bonus", "dividend", "dividend"], "factor": [0.5, None, None], "amount": [None, 80.0, None]})
    res = B.reconcile(acts, pd.concat([bars(500011, "2024-03-15", 1.0, gap=False)]), calendar=pd.bdate_range("2024-01-01", "2024-06-30")).set_index("id")
    assert res.loc[1, "classification"] == "needs_review" and "no price bars" in res.loc[1, "reason"]
    assert res.loc[2, "classification"] == "needs_review" and "above 25%" in res.loc[2, "reason"]
    assert res.loc[3, "classification"] == "needs_review" and "could not be read" in res.loc[3, "reason"]


def test_the_column_mapping_is_exposed_so_a_person_can_check_it_against_the_real_file():
    ex = B.read_export(NL.join(["Security Code,Security Name,Ex Date,Purpose,Weird Extra", "500001,A,15 Mar 2024,Bonus issue 1:1,zzz"]).encode())
    assert ex.mapping["scrip_code"] == "Security Code" and ex.mapping["purpose"] == "Purpose" and ex.mapping["ex_date"] == "Ex Date"
    assert ex.unmatched == ["Weird Extra"] and "record_date" not in ex.mapping
    assert ex.frame["raw_row"].iloc[0]["Weird Extra"] == "zzz"


def test_same_day_actions_are_tested_together_and_verdicts_distinguish_contradiction_from_doubt():
    acts = pd.DataFrame({"id": [1, 2, 3, 4, 5, 6], "scrip_code": [600001, 600001, 600002, 600003, 600004, 600004],
                         "ex_date": pd.to_datetime(["2024-03-15"] * 6),
                         "purpose": ["Bonus issue 1:1", "Stock Split From Rs.5/- to Rs.1/-", "Bonus issue 1:1", "Bonus issue 1:1", "Bonus issue 1:1", "Stock Split From Rs.10/- to Rs.2/-"],
                         "action_type": ["bonus", "split", "bonus", "bonus", "bonus", "split"], "factor": [0.5, 0.2, 0.5, 0.5, 0.5, 0.2], "amount": [None] * 6})
    px = pd.concat([bars(600001, "2024-03-15", 0.1),                  # bonus 1:1 and split 5->1 on one day: the price falls to 0.1x, the PRODUCT of the factors
                    bars(600002, "2024-03-15", 1.0, gap=False),     # listed bonus, no gap at all: the prices contradict the listing
                    bars(600003, "2024-03-15", 0.4),                # gap 0.4 vs 0.5: 20% off, outside the bound but not contradictory: a person decides
                    bars(600004, "2024-03-15", 0.1)])               # same pair of actions again: 0.5 x 0.2 = 0.1
    res = B.reconcile(acts, px, calendar=pd.bdate_range("2024-01-01", "2024-06-30")).set_index("id")
    assert res.loc[1, "classification"] == res.loc[2, "classification"] == "confirmed" and "2 actions share this ex-date" in res.loc[1, "reason"]
    assert res.loc[3, "classification"] == "rejected" and "more than 25%" in res.loc[3, "reason"]
    assert res.loc[4, "classification"] == "needs_review" and "off the listed" in res.loc[4, "reason"]
    assert res.loc[5, "classification"] == res.loc[6, "classification"] == "confirmed"


def test_actions_without_readable_terms_and_non_price_rows_are_typed_and_deferred():
    c = B.classify_purpose
    assert c("Consolidation of Shares")["action_type"] == "consolidation" and c("Consolidation of Shares")["factor"] is None
    assert c("Sub Division of Equity shares")["action_type"] == "split"
    assert [c(x)["action_type"] for x in ("Income Distribution (InvIT)", "InvIT - Return of Capital", "Resolution Plan -Suspension", "E.G.M.", "Annual Book Closure")] ==         ["distribution", "distribution", "suspension", "administrative", "administrative"]
    acts = pd.DataFrame({"id": [1, 2, 3], "scrip_code": [1, 2, 3], "ex_date": pd.to_datetime(["2024-03-15"] * 3), "purpose": ["Consolidation of Shares", "E.G.M.", "Income Distribution REITs"],
                         "action_type": ["consolidation", "administrative", "distribution"], "factor": [None] * 3, "amount": [None] * 3})
    res = B.reconcile(acts, pd.DataFrame(columns=["scrip_code", "date", "open", "close"]), calendar=None).set_index("id")
    assert set(res["classification"]) == {"needs_review"} and "without readable terms" in res.loc[1, "reason"] and "not a price-affecting" in res.loc[2, "reason"]


def test_agm_is_matched_as_a_word_and_not_inside_other_words():
    assert B.classify_purpose("AGM")["action_type"] == "administrative" and B.classify_purpose("Annual General Meeting")["action_type"] == "administrative"
    assert B.classify_purpose("Magma Fincorp Bonus issue 1:1")["action_type"] == "bonus"
