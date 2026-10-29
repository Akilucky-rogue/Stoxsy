"""The owner's Bucket A decisions as an INPUT to the rebuild: confirmed keep the factor (tagged), held keep it provisionally (tagged, never called validated), rejected lose it; nothing is ignored."""
import pandas as pd
import pytest

from qsde.ingestion import bucket_a_decisions as BA


def events():
    return pd.DataFrame({"symbol": ["AAA", "BBB", "CCC", "DDD", "AAA"], "ex_date": pd.to_datetime(["2022-01-03", "2023-02-01", "2024-03-04", "2024-05-06", "2020-01-01"]),
                         "applied": [True, True, True, True, True], "applied_research": [False, False, False, False, False], "price_factor": [0.5, 0.5, 0.5, 0.5, 0.5]})


def decisions(held="keep_listed_factor_flagged_unreviewed"):
    base = [{"symbol": "AAA", "ex_date": "2022-01-03", "decision": "confirm", "reason": "ordinary move"},
            {"symbol": "BBB", "ex_date": "2023-02-01", "decision": "needs_more_evidence", "reason": "thin volume"},
            {"symbol": "CCC", "ex_date": "2024-03-04", "decision": "reject", "reason": "residual 4.5 sigma"},
            {"symbol": "ZZZ", "ex_date": "2024-01-01", "decision": "confirm", "reason": "no such event"}]
    return {"decisions": base, "rebuild_policy": {"held": held}}


def test_option_b_confirmed_and_held_keep_their_factor_rejected_loses_it():
    ev, rep = BA.apply_policy(events(), decisions())
    by = {(r.symbol, str(r.ex_date.date())): r for r in ev.itertuples()}
    a, b, c, d, a_old = by[("AAA", "2022-01-03")], by[("BBB", "2023-02-01")], by[("CCC", "2024-03-04")], by[("DDD", "2024-05-06")], by[("AAA", "2020-01-01")]
    assert (a.applied, a.review_status) == (True, "owner_confirmed")
    assert (b.applied, b.review_status) == (True, "applied_unreviewed")                      # provisional, not validated
    assert (c.applied, c.applied_research, c.review_status) == (False, False, "adjustment_removed") and "4.5 sigma" in c.review_note
    assert d.applied and d.review_status is None and a_old.applied and a_old.review_status is None       # undecided and other-year events are untouched
    assert rep["changed"] == [("CCC", "2024-03-04", "reject")] and rep["unmatched"] == [("ZZZ", "2024-01-01")] and rep["matched"] == 3
    assert rep["counts"] == {"owner_confirmed": 1, "applied_unreviewed": 1, "adjustment_removed": 1}


def test_the_same_symbol_on_another_date_is_a_different_event():
    ev, _ = BA.apply_policy(events(), decisions())
    assert ev[(ev.symbol == "AAA") & (ev.ex_date == "2020-01-01")]["review_status"].isna().all()


def test_the_alternative_policy_removes_held_events_too():
    ev, rep = BA.apply_policy(events(), decisions(held="remove"))
    b = ev[ev.symbol == "BBB"].iloc[0]
    assert (b.applied, b.review_status) == (False, "adjustment_removed") and rep["held_policy"] == "remove" and ("BBB", "2023-02-01", "needs_more_evidence") in rep["changed"]


def test_an_empty_events_table_reports_every_decision_as_unmatched():
    ev, rep = BA.apply_policy(events().iloc[0:0], decisions())
    assert ev.empty and len(rep["unmatched"]) == 4 and "review_status" in ev.columns


def test_the_recorded_policy_file_is_option_b_and_complete():
    d = BA.load()
    assert d["rebuild_policy"]["held"] == BA.HELD_KEEP and d["rebuild_policy"]["rejected"] == "remove_adjustment"
    assert sum(1 for x in d["decisions"] if x["decision"] == "reject") == 4 and len(d["decisions"]) == 69


@pytest.mark.real_data
def test_review_queue_lists_the_34_held_events_by_universe_impact_and_the_4_rejected_with_their_raw_gap():
    q = BA.review_queue()
    assert len(q["held"]) == 34 and len(q["rejected"]) == 4 and q["policy"]["held"] == BA.HELD_KEEP
    in_u = [h.get("in_universe", False) for h in q["held"]]
    assert in_u == sorted(in_u, reverse=True)                                                  # universe names first
    assert all(r["raw_gap"] is not None and r["raw_gap"] < -0.15 for r in q["rejected"])       # the raw corporate-action drop is visible in ohlcv now
    assert "NOT validated" in q["note"]


@pytest.mark.real_data
def test_the_rebuilt_series_carries_the_review_tags_and_rejected_events_are_not_applied():
    from qsde.db.connection import read_sql

    t = read_sql("SELECT review_status, applied, count(*) AS n FROM corp_action_events WHERE review_status IS NOT NULL GROUP BY 1, 2")
    got = {(r.review_status, bool(r.applied)): int(r.n) for r in t.itertuples()}
    assert got == {("owner_confirmed", True): 31, ("applied_unreviewed", True): 34, ("adjustment_removed", False): 4}
