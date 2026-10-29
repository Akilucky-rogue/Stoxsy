"""Fund snapshot: basis labels, data-quality badge, peer context (pure functions)."""
import numpy as np
import pandas as pd
import pytest

from qsde.funds.snapshot import MIN_PEERS, add_peer_context, basis_of, peer_key, quality, scheme_snapshot


def navs(n=1600, daily=0.0004, start="2020-01-01"):
    return pd.Series(10 * (1 + daily) ** np.arange(n), index=pd.bdate_range(start, periods=n))


META = {"scheme_code": 1, "is_growth": True, "is_etf": False, "is_direct": True, "asset_class": "equity", "sub_category": "Large Cap Fund", "fund_group": "standard"}


def test_basis_labels_never_mix():
    assert basis_of(True, False) == "total_return_after_expenses"
    assert basis_of(False, False) == "price_return_only"
    assert basis_of(None, True) == "price_return"
    assert basis_of(None, False) == "option_unknown"


def test_quality_badge_levels_and_reasons():
    ok = quality(navs(), META, "total_return_after_expenses")
    assert ok["level"] == "ok" and ok["reasons"] == []
    jump = navs()
    jump.iloc[1200:] *= 100
    q = quality(jump, META, "total_return_after_expenses")
    assert q["level"] == "poor" and "above 50%" in q["reasons"][0] and q["flags"]["unit_change_moves_5y"] == 1
    gappy = navs().drop(navs().index[1300:1340])
    g = quality(gappy, META, "total_return_after_expenses")
    assert g["level"] == "caution" and g["flags"]["gaps_over_30d_3y"] == 1
    young = quality(navs(n=120), META, "total_return_after_expenses")
    assert young["level"] == "caution" and "no 1-year figure" in " ".join(young["reasons"])
    assert quality(navs(), {**META, "fund_group": "unclassified", "unclassified_reason": "x"}, "total_return_after_expenses")["level"] == "poor"
    assert quality(navs(), {**META, "asset_class": "other"}, "total_return_after_expenses")["level"] == "caution"


def test_snapshot_carries_statuses_and_sortable_copies():
    s = scheme_snapshot(navs(), META)
    assert s["basis"] == "total_return_after_expenses" and s["returns"]["1Y"]["status"] == "ok" and s["ret_1y"] == pytest.approx(s["returns"]["1Y"]["annualised"])
    assert s["vol_3y"] is not None and s["maxdd_3y"] <= 0 and s["_peer_key"] == "equity|Large Cap Fund|direct"
    short = scheme_snapshot(navs(n=40), META)
    assert short["ret_1y"] is None and short["returns"]["1Y"]["status"] == "insufficient_history" and short["vol_3y"] is None
    assert scheme_snapshot(navs(n=1), META) is None


def test_peer_context_needs_enough_like_for_like_peers_and_excludes_poor_and_idcw():
    rows = []
    for i in range(MIN_PEERS + 2):
        rows.append(scheme_snapshot(navs(daily=0.0002 + i * 0.00005), {**META, "scheme_code": i + 1}))
    bad = navs(daily=0.01)
    bad.iloc[1000:] *= 100
    rows.append(scheme_snapshot(bad, {**META, "scheme_code": 99}))
    idcw = scheme_snapshot(navs(daily=0.005), {**META, "scheme_code": 98, "is_growth": False})
    rows.append(idcw)
    add_peer_context(rows)
    best, worst = rows[MIN_PEERS + 1], rows[0]
    assert best["peers"]["horizons"]["1Y"]["percentile"] == pytest.approx(1.0) and worst["peers"]["horizons"]["1Y"]["percentile"] == pytest.approx(0.0)
    assert best["peers"]["group_size"] == MIN_PEERS + 2                       # the poor-quality and IDCW lines are not in the group
    assert rows[-2]["peers"] is None and idcw["peers"] is None and idcw["_peer_key"] is None
    few = [scheme_snapshot(navs(daily=0.0002 + i * 0.0001), {**META, "scheme_code": i + 1}) for i in range(MIN_PEERS - 1)]
    add_peer_context(few)
    assert all(r["peers"] is None for r in few)
    assert peer_key({"asset_class": "equity"}, "price_return") == "etf|equity"
