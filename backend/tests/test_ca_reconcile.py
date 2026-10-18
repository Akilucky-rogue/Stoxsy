"""NSE corporate-action reconciliation: confirmed / needs_review / rejected, evidence only."""
import numpy as np
import pandas as pd
import pytest

from qsde.ingestion.ca_reconcile import parse_dividend, parse_rights, reconcile_actions

D = pd.bdate_range("2024-01-01", periods=120)


def px(sym, closes, start=0):
    d = D[start:start + len(closes)]
    c = np.array(closes, float)
    return pd.DataFrame({"symbol": sym, "date": d, "open": c, "close": c})


def action(i, sym, cat, subject, ex):
    return {"id": i, "symbol": sym, "category": cat, "subject": subject, "ex_date": ex}


@pytest.fixture
def prim():
    return pd.concat([px("DIVCO", [100.0] * 120), px("SPLITCO", [100.0] * 60 + [10.0] * 60),
                      px("FLATCO", [100.0] * 120), px("RIGHTCO", [100.0] * 120), px("DEMCO", [100.0] * 60 + [60.0] * 60)])


def run(prim, rows):
    return reconcile_actions(pd.DataFrame(rows), prim, None, D.to_numpy()).set_index("action_id")


def test_parsers():
    assert parse_dividend("Interim Dividend - Rs 6/- Per Share") == (6.0, 1)
    assert parse_dividend("Dividend - Rs 2.50 Per Share + Special Dividend Re 1") == (3.5, 2)
    assert parse_dividend("Annual General Meeting") == (None, 0)
    assert parse_rights("Rights 2:21 @ Premium Rs 748/-") == ("2:21", 748.0)
    assert parse_rights("Rights Issue") == (None, None)


def test_dividend_confirmed_review_pending_and_unknown_symbol(prim):
    r = run(prim, [action(1, "DIVCO", "DIVIDEND", "Dividend - Rs 5 Per Share", D[40]),
                   action(2, "DIVCO", "DIVIDEND", "Dividend - Rs 60 Per Share", D[40]),
                   action(3, "DIVCO", "DIVIDEND", "Final Dividend", D[40]),
                   action(4, "DIVCO", "DIVIDEND", "Dividend - Rs 5 Per Share", D[119] + pd.Timedelta(days=30)),
                   action(5, "NOBARS", "DIVIDEND", "Dividend - Rs 5 Per Share", D[40])])
    assert r.loc[1, "classification"] == "confirmed" and r.loc[1, "yield_pct"] == pytest.approx(5.0)
    assert r.loc[2, "classification"] == "needs_review" and "60%" in r.loc[2, "reason"]
    assert r.loc[3, "classification"] == "needs_review" and "not parseable" in r.loc[3, "reason"]
    assert r.loc[4, "classification"] == "needs_review" and "pending" in r.loc[4, "reason"]
    assert r.loc[5, "classification"] == "rejected" and "no price bars" in r.loc[5, "reason"]


def test_split_confirmed_by_the_gap_and_rejected_when_prices_show_nothing(prim):
    r = run(prim, [action(1, "SPLITCO", "SPLIT_BONUS", "Face Value Split (Sub-Division) - From Rs 10/- Per Share To Re 1/- Per Share", D[60]),
                   action(2, "FLATCO", "SPLIT_BONUS", "Face Value Split (Sub-Division) - From Rs 10/- Per Share To Re 1/- Per Share", D[60])])
    assert r.loc[1, "classification"] == "confirmed" and r.loc[1, "listed_factor"] == pytest.approx(0.1)
    assert r.loc[2, "classification"] == "rejected" and "no price gap" in r.loc[2, "reason"]


def test_rights_identified_but_no_price_factor_is_invented(prim):
    r = run(prim, [action(1, "RIGHTCO", "RIGHTS", "Rights 2:21 @ Premium Rs 748/-", D[50]),
                   action(2, "RIGHTCO", "RIGHTS", "Rights Issue", D[50])])
    assert r.loc[1, "classification"] == "confirmed" and r.loc[1, "terms"] == "2:21 @ premium 748.0" and pd.isna(r.loc[1, "listed_factor"])
    assert r.loc[2, "classification"] == "needs_review"


def test_demergers_always_need_individual_review_with_the_gap_as_evidence(prim):
    r = run(prim, [action(1, "DEMCO", "DEMERGER", "Demerger", D[60])])
    row = r.loc[1]
    assert row.classification == "needs_review" and row.observed_ratio == pytest.approx(0.6) and "-40.0%" in row.reason
