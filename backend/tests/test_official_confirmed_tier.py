"""The `official_confirmed` approval tier: an NSE record corroborates, but never overrides a hard timing/factor failure."""
import numpy as np
import pandas as pd
import pytest

from qsde.ingestion.corp_actions_approvals import OFFICIAL_TIER, apply_approvals, rename_reachable

D = pd.bdate_range("2024-01-01", periods=60)


def prim(sym, cut, ratio=0.1, drop=()):
    c = np.where(np.arange(len(D)) < cut, 100.0, 100.0 * ratio)
    df = pd.DataFrame({"symbol": sym, "date": D, "open": c, "close": c})
    return df[~df["date"].isin(pd.to_datetime(list(drop)))].reset_index(drop=True)


def unconfirmed(sym, listed):
    return pd.DataFrame([dict(exchange="NSE", symbol=sym, ex_date=listed, prev_session=listed, close_prev_session=np.nan,
                              open_ex_date=np.nan, close_ex_date=np.nan, open_ratio=np.nan, close_ratio=np.nan, volume_ratio=np.nan,
                              persistence_ratio=np.nan, kind="SOURCE_UNCONFIRMED", snapped_ratio="face value 10 -> 1", price_factor=np.nan,
                              applied=False, method="source_confirmed_v2", source="nse_corp_actions", source_purpose="Face Value Split",
                              ex_date_shift=None, applied_research=False, confidence="high")])


def nse(sym, ex, factor=0.1, isin="INE000A01010"):
    return pd.DataFrame([{"symbol": sym, "category": "SPLIT_BONUS", "factor": factor, "ex_date": ex,
                          "isin": isin, "known_at": pd.Timestamp("2026-10-18T15:00:00Z")}])


def approval(gap, listed, **kw):
    a = {"symbol": "THIN", "listed_ex_date": str(listed.date()), "gap_date": str(gap.date()), "factor": 0.1, "decision": "approve",
         "tier": OFFICIAL_TIER, "evidence_pack": "THIN.md"}
    a.update(kw)
    return a


@pytest.fixture
def pack(tmp_path):
    (tmp_path / "THIN.md").write_text("evidence", encoding="utf-8")
    return tmp_path


def run(p, a, nse_df, renames=None, pack_dir=None, listed=None):
    ev = unconfirmed("THIN", listed or D[28].date())
    return apply_approvals(ev, p, [a], nse_records=nse_df, renames=renames, evidence_dir=pack_dir)


def test_confirmed_by_the_official_record_and_the_evidence_is_stored(pack):
    p = prim("THIN", 30, drop=[D[28], D[29]])                       # no trading on the ex-date or the day before; gap bar = D[30]
    ev, rep = run(p, approval(D[30], D[28]), nse("THIN", D[29]), pack_dir=pack)
    r = ev.iloc[0]
    assert rep["applied"] and bool(r.applied) and r.confidence == OFFICIAL_TIER and r.approval_tier == OFFICIAL_TIER
    assert r["isin"] == "INE000A01010" and r.source_url.startswith("https://www.nseindia.com/api/") and r.evidence_ref == "THIN.md"
    assert pd.Timestamp(r.retrieved_at) == pd.Timestamp("2026-10-18T15:00:00Z")


def test_no_matching_official_record_means_no_second_tier(pack):
    p = prim("THIN", 30)
    for bad in (None, nse("OTHER", D[30]), nse("THIN", D[30], factor=0.2)):
        ev, rep = run(p, approval(D[30], D[28]), bad, pack_dir=pack)
        assert not rep["applied"] and rep["refused"] and ev.iloc[0].kind == "SOURCE_UNCONFIRMED"


def test_official_ex_date_must_lie_in_the_untraded_window_or_on_the_gap_bar(pack):
    p = prim("THIN", 30)                                            # traded every day: previous bar is D[29]
    ev, rep = run(p, approval(D[30], D[28]), nse("THIN", D[25]), pack_dir=pack)       # ex-date long before the last pre-gap bar
    assert not rep["applied"] and "official ex-date" in rep["refused"][0]
    ev2, rep2 = run(p, approval(D[30], D[28]), nse("THIN", D[30]), pack_dir=pack)     # ex-date ON the gap bar
    assert rep2["applied"]


def test_the_official_record_never_overrides_a_hard_bound(pack):
    # NDRAUTO pattern: perfect official record, but the factor is 7.9% off
    p = prim("THIN", 30, ratio=0.0921)
    ev, rep = run(p, approval(D[30], D[28]), nse("THIN", D[30]), pack_dir=pack)
    assert not rep["applied"] and "factor error" in rep["refused"][0]
    # ... and a long halt (more than 5 untraded sessions) is refused as well
    halt = [D[i] for i in range(22, 30)]
    p2 = prim("THIN", 30, drop=halt)
    mkt = prim("MKT", 30)
    ev2, rep2 = apply_approvals(unconfirmed("THIN", D[28].date()), pd.concat([p2, mkt]), [approval(D[30], D[28])],
                                nse_records=nse("THIN", D[29]), evidence_dir=pack)
    assert not rep2["applied"] and "untraded" in rep2["refused"][0]


def test_timing_is_measured_from_the_official_ex_date(pack):
    # listed date is 6 sessions before the gap bar (would breach the 5-session bound) but the OFFICIAL ex-date is the day before it
    p = prim("THIN", 30)
    ev, rep = run(p, approval(D[30], D[24]), nse("THIN", D[30]), pack_dir=pack, listed=D[24].date())
    assert rep["applied"]
    ev_old, rep_old = run(p, approval(D[30], D[24], tier=None), None, pack_dir=pack, listed=D[24].date())
    assert not rep_old["applied"]                                    # the price-only tier still applies the original rule


def test_rename_continuity_must_be_official(pack):
    p = prim("OLDCO", 30)
    ev0 = unconfirmed("NEWCO", D[28].date())
    a = approval(D[30], D[28], symbol="NEWCO", apply_to_symbol="OLDCO")
    off = pd.DataFrame([{"old_symbol": "OLDCO", "new_symbol": "NEWCO"}])
    ev, rep = apply_approvals(ev0, p, [a], nse_records=nse("NEWCO", D[30]), renames=off, evidence_dir=pack)
    assert rep["applied"] and ev[ev["applied"]].iloc[0].symbol == "OLDCO"
    ev2, rep2 = apply_approvals(ev0, p, [a], nse_records=nse("NEWCO", D[30]), renames=None, evidence_dir=pack)
    assert not rep2["applied"] and "not an official NSE rename" in rep2["refused"][0]
    assert rename_reachable(pd.DataFrame([{"old_symbol": "A", "new_symbol": "B"}, {"old_symbol": "B", "new_symbol": "C"}]), "A", "C")


def test_missing_evidence_pack_blocks_the_tier(tmp_path):
    p = prim("THIN", 30)
    ev, rep = run(p, approval(D[30], D[28]), nse("THIN", D[30]), pack_dir=tmp_path)           # empty directory
    assert not rep["applied"] and "evidence pack" in rep["refused"][0]
