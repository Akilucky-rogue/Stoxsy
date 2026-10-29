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


def test_repository_file_reflects_the_owner_decisions():
    from collections import Counter

    from qsde.ingestion.corp_actions_approvals import load_approvals
    dec = load_approvals()
    by = {(x["symbol"], x.get("apply_to_symbol"), x["listed_ex_date"]): x for x in dec}
    # held: official record does not override a hard timing / factor failure
    assert by[("NDRAUTO", None, "2023-07-24")]["decision"] == "rejected_pending_evidence"
    assert by[("VERTOZ", None, "2025-06-24")]["decision"] == "reject"
    for k in (("UEL", None, "2025-10-08"), ("DRCSYSTEMS", None, "2022-03-16")):
        x = by[k]
        assert x["decision"] == "approve" and x["tier"] == "official_confirmed" and x["evidence"]["isin"] and x["evidence_pack"]
        assert x["history"], "the original approval trail must be preserved"
    assert by[("DRCSYSTEMS", None, "2022-03-16")]["history"][0]["state"]["confidence"] == "medium"      # earlier state kept
    tiers = Counter((x["decision"], x.get("tier")) for x in dec)
    assert tiers[("approve", "official_confirmed")] == 10 and tiers[("approve", None)] == 15
    assert not [x for x in dec if x["decision"] == "manual_review"]
    fp = approvals_fingerprint()
    assert fp["approved"] == 25 and fp["sha256"]


def test_confidence_from_the_approval_is_stored_on_the_event():
    prim, d = _prim()
    a = _approval(d)
    a["confidence"] = "medium"
    ev, rep = apply_approvals(_unconfirmed("THIN", d[13].date()), prim, [a])
    assert ev.iloc[0].confidence == "medium" and bool(ev.iloc[0].applied)


def test_rejected_pending_evidence_is_never_applied():
    prim, d = _prim()
    ev, rep = apply_approvals(_unconfirmed("THIN", d[13].date()), prim, [_approval(d, "rejected_pending_evidence")])
    assert rep["rejected"] and not rep["applied"] and not bool(ev.iloc[0].applied)


class TestHardBounds:
    """NDRAUTO: gap 19 sessions after the listing, factor 7.8% off. Such an approval must be refused by the code itself."""

    def test_check_bounds_flags_each_violation(self):
        from qsde.ingestion.corp_actions_approvals import check_bounds
        assert check_bounds(0.5, 0.4607, 19, 25)                        # NDRAUTO: factor, timing and halt
        assert any("factor error" in v for v in check_bounds(0.5, 0.461, 0, 0))
        assert any("market sessions from the listed" in v for v in check_bounds(0.5, 0.5, 6, 0))
        assert any("untraded" in v for v in check_bounds(0.5, 0.5, 0, 6))
        assert check_bounds(0.1, 0.0965, 3, 3) == []                    # DRCSYSTEMS passes (3.5% off, 3 sessions)
        assert check_bounds(0.1, 0.102, -1, 1) == []                    # DOLPHIN
        assert check_bounds(1 / 3, 0.3168, 3, 1) == []                  # UEL (4.96% off: inside, just)

    def test_mild_factors_need_a_tighter_match(self):
        from qsde.ingestion.corp_actions_approvals import check_bounds
        assert check_bounds(0.909, 0.98, 0, 0)                          # 7.8% off a 1:10 bonus
        assert check_bounds(0.909, 0.92, 0, 0) == []

    def test_apply_refuses_an_approval_that_is_too_late(self):
        prim, d = _prim()
        late = _approval(d, gap=d[15])
        late["listed_ex_date"] = str(d[5].date())                       # listed 10 sessions before the gap bar
        ev, rep = apply_approvals(_unconfirmed("THIN", d[5].date()), prim, [late])
        assert not rep["applied"] and any("hard bounds" in r for r in rep["refused"])
        assert ev.iloc[0].kind == "SOURCE_UNCONFIRMED"

    def test_apply_refuses_after_a_long_halt(self):
        d0 = pd.bdate_range("2024-01-01", periods=30)
        prim, d = _prim(drop=list(d0[8:15]))                              # 7 untraded sessions right before the gap bar
        market, _ = _prim("MKT")                                          # the market calendar comes from ALL symbols
        prim = pd.concat([prim, market], ignore_index=True)
        ev, rep = apply_approvals(_unconfirmed("THIN", d[13].date()), prim, [_approval(d)])
        assert not rep["applied"] and any("untraded" in r for r in rep["refused"])


class TestRenamedStocks:
    def test_action_keyed_under_the_new_symbol_is_applied_to_the_symbol_that_traded(self):
        prim, d = _prim("OLDCO")                                           # the series with the real gap
        unc = _unconfirmed("NEWCO", d[13].date())                          # the feed keyed it under today's name
        stale = unc.copy()
        stale["symbol"], stale["source"], stale["kind"] = "OLDCO", "price_inferred", "SPLIT_BONUS"
        stale["applied"], stale["applied_research"], stale["confidence"] = False, True, "low"
        stale["ex_date"] = d[15].date()
        ev0 = pd.concat([unc, stale], ignore_index=True)
        a = {"symbol": "NEWCO", "apply_to_symbol": "OLDCO", "listed_ex_date": str(d[13].date()), "gap_date": str(d[15].date()),
             "factor": 0.1, "decision": "approve"}
        ev, rep = apply_approvals(ev0, prim, [a])
        assert rep["applied"] and not rep["refused"]
        applied = ev[ev["applied"]]
        assert list(applied["symbol"]) == ["OLDCO"] and applied.iloc[0].method == APPROVALS_VERSION
        assert not ((ev["source"] == "price_inferred") & (ev["symbol"] == "OLDCO")).any()      # superseded
        assert not ((ev["symbol"] == "NEWCO") & (ev["kind"] == "SOURCE_UNCONFIRMED")).any()    # the listing is resolved
