"""Owner-approved corporate-action decisions applied on top of the source-confirmed events."""
import numpy as np
import pandas as pd
import pytest

from qsde.ingestion import corp_actions as ca
from qsde.ingestion.corp_actions_approvals import APPROVALS_VERSION, apply_approvals, approvals_fingerprint


def _prim(sym="THIN", drop=()):
    d = pd.bdate_range("2024-01-01", periods=30)
    close = np.where(np.arange(30) < 15, 100.0, 10.0)                  # 1:10 split at index 15
    df = pd.DataFrame({"symbol": sym, "date": d, "open": close, "high": close, "low": close, "close": close, "volume": 1000})
    return df[~df["date"].isin(pd.to_datetime(list(drop)))].reset_index(drop=True), d


def _unconfirmed(sym, listed):
    return pd.DataFrame([dict(exchange="NSE", symbol=sym, ex_date=listed, prev_session=listed, close_prev_session=np.nan,
                              open_ex_date=np.nan, close_ex_date=np.nan, open_ratio=np.nan, close_ratio=np.nan,
                              volume_ratio=np.nan, persistence_ratio=np.nan, kind="SOURCE_UNCONFIRMED",
                              snapped_ratio="face value 10 -> 1", price_factor=np.nan, applied=False, method="source_confirmed_v2",
                              source="nse_corp_actions", source_purpose="Face Value Split", ex_date_shift=None,
                              applied_research=False, confidence="high")])


def _approval(d, decision="approve", gap=None, factor=0.1):
    return {"symbol": "THIN", "listed_ex_date": str(d[13].date()), "gap_date": str((gap or d[15]).date()),
            "factor": factor, "decision": decision}


def test_approved_action_is_applied_at_the_observed_gap_date_with_the_approval_method():
    prim, d = _prim()
    ev, rep = apply_approvals(_unconfirmed("THIN", d[13].date()), prim, [_approval(d)])
    r = ev.iloc[0]
    assert rep["applied"] and r.kind == "SPLIT_BONUS" and bool(r.applied) and r.method == APPROVALS_VERSION
    assert pd.Timestamp(r.ex_date) == d[15] and r.price_factor == pytest.approx(0.1) and r.confidence == "high"
    assert r.ex_date_shift == 2
    out = ca.adjust_prices(prim.assign(deliv_per=np.nan, date=prim["date"].dt.date), ev, "applied")   # prod frames carry python dates
    assert out["close"].tolist() == pytest.approx([10.0] * 30)          # continuous after adjustment


def test_untraded_sessions_before_the_gap_bar_are_recorded():
    prim, d = _prim(drop=[d_ for d_ in pd.bdate_range("2024-01-12", periods=3)])
    ev, rep = apply_approvals(_unconfirmed("THIN", d[13].date()), prim, [_approval(d)])
    assert "untraded" in ev.iloc[0].snapped_ratio and "3 untraded" in rep["applied"][0] or "untraded" in rep["applied"][0]


def test_guard_refuses_when_the_data_no_longer_matches_the_approved_factor():
    prim, d = _prim()
    ev, rep = apply_approvals(_unconfirmed("THIN", d[13].date()), prim, [_approval(d, factor=0.5)])
    assert not rep["applied"] and rep["refused"] and ev.iloc[0].kind == "SOURCE_UNCONFIRMED" and not bool(ev.iloc[0].applied)


def test_reject_leaves_the_row_unapplied_and_is_recorded():
    prim, d = _prim()
    ev, rep = apply_approvals(_unconfirmed("THIN", d[13].date()), prim, [_approval(d, "reject")])
    assert rep["rejected"] and ev.iloc[0].kind == "SOURCE_UNCONFIRMED" and not bool(ev.iloc[0].applied)


def test_approval_without_a_matching_unconfirmed_row_is_refused():
    prim, d = _prim()
    ev, rep = apply_approvals(_unconfirmed("OTHER", d[13].date()), prim, [_approval(d)])
    assert rep["refused"] and not rep["applied"]


def test_repository_file_has_four_approvals_and_vertoz_rejected():
    from qsde.ingestion.corp_actions_approvals import load_approvals
    dec = {x["symbol"]: x["decision"] for x in load_approvals()}
    assert dec == {"DOLPHIN": "approve", "DRCSYSTEMS": "approve", "NDRAUTO": "approve", "UEL": "approve", "VERTOZ": "reject"}
    fp = approvals_fingerprint()
    assert fp["approved"] == 4 and fp["rejected"] == 1 and fp["sha256"]
