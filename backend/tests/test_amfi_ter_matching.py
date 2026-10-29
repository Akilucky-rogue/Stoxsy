"""Matching of AMFI TER schemes (NSDL codes) to mf_scheme_master, tested only on the real file and the real master (read-only SELECTs)."""
import re
import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from amfi_ter_support import require_real_file  # noqa: E402
from qsde.db.connection import read_sql  # noqa: E402
from qsde.ingestion import amfi_ter as T  # noqa: E402

pytestmark = pytest.mark.real_data
TODAY = date(2026, 10, 29)
OCTOBER_SHA16 = "0C1DD64F0A4EB231"
MASTER_REASONS = {"not_in_file", "ambiguous_key", "plan_unknown", "plan_not_offered"}


@pytest.fixture(scope="module")
def world():
    f = require_real_file()
    ex = T.read_export(f.read_bytes(), f.name)
    master = read_sql("SELECT scheme_code, name, amc, category_raw, is_direct, is_growth, last_nav_date FROM mf_scheme_master")
    if master.empty:
        pytest.skip("mf_scheme_master is empty")
    ter = T.ter_schemes(ex.frame)
    return ex, ter, master, T.match_schemes(ter, master, TODAY)


def _active(master):
    return master[pd.to_datetime(master["last_nav_date"]) >= pd.Timestamp(TODAY) - pd.Timedelta(days=90)]


def test_ter_schemes_has_one_row_per_code_with_offered_plans(world):
    ex, ter, _, _ = world
    assert ter["nsdl_code"].is_unique and len(ter) == ex.frame["nsdl_code"].nunique()
    assert {"nsdl_code", "name", "scheme_category", "r_offered", "d_offered"} <= set(ter.columns)
    one = ex.frame.groupby("nsdl_code")["r_total"].apply(lambda s: s.notna().any())
    assert ter.set_index("nsdl_code")["r_offered"].equals(one.reindex(ter["nsdl_code"]).rename("r_offered"))


def test_every_ter_scheme_is_mapped_or_listed(world):
    _, ter, _, m = world
    listed = set(m.unmatched.query("side == 'ter'")["nsdl_code"])
    mapped = set(m.mapped["nsdl_code"])
    assert mapped | listed == set(ter["nsdl_code"]) and not (mapped & listed)
    assert set(m.unmatched.query("side == 'ter'")["reason"]) <= {"no_name_key", "ambiguous_key", "no_plan_match"}


def test_mapping_uses_the_right_plan(world):
    _, ter, master, m = world
    j = m.mapped.merge(master[["scheme_code", "is_direct"]], on="scheme_code").merge(ter[["nsdl_code", "r_offered", "d_offered"]], on="nsdl_code")
    assert not j.query("plan == 'regular' and is_direct == True").shape[0] and not j.query("plan == 'direct' and is_direct == False").shape[0]
    plan_less = j[j["is_direct"].isna()]
    assert ((plan_less["plan"] == "regular") & plan_less["r_offered"] & ~plan_less["d_offered"] | (plan_less["plan"] == "direct") & plan_less["d_offered"] & ~plan_less["r_offered"]).all()
    assert (((j["plan"] == "regular") & j["r_offered"]) | ((j["plan"] == "direct") & j["d_offered"])).all()          # never mapped to a plan that is not offered


def test_no_master_scheme_gets_two_codes_for_one_plan(world):
    _, _, _, m = world
    assert m.mapped.groupby(["scheme_code", "plan"])["nsdl_code"].nunique().max() == 1


def test_unmatched_master_rows_have_reasons(world):
    _, _, master, m = world
    act = set(_active(master)["scheme_code"])
    um = m.unmatched.query("side == 'master'")
    assert set(um["reason"]) <= MASTER_REASONS
    assert act <= set(m.mapped["scheme_code"]) | set(um["scheme_code"].dropna().astype(int))
    assert set(um["scheme_code"].dropna().astype(int)) <= act                      # only active schemes are listed as gaps


def test_shared_keys_follow_the_option_rule(world):
    _, ter, master, m = world
    key = ter["name"].map(T.name_key)
    shared = ter[key.map(key.value_counts()) > 1]
    if shared.empty:
        pytest.skip("the real file has no name key shared by two codes")
    oc = shared["name"].map(T.option_class)
    for _, grp in shared.assign(k=key, oc=oc).groupby("k"):
        distinct = grp["oc"].notna().all() and grp["oc"].nunique() == len(grp)
        for r in grp.itertuples():
            got = m.mapped[m.mapped["nsdl_code"] == r.nsdl_code].merge(master[["scheme_code", "is_growth"]], on="scheme_code")
            if distinct:
                assert not ((m.unmatched["nsdl_code"] == r.nsdl_code) & (m.unmatched["reason"] == "ambiguous_key")).any()
                assert (got["is_growth"] == (r.oc == "growth")).all()
            else:
                assert got.empty


def test_name_key_collapses_real_plan_and_option_variants_without_crossing_amcs(world):
    _, _, master, _ = world
    act = _active(master).assign(k=lambda d: d["name"].map(T.name_key))
    by_key = act.groupby("k").agg(amcs=("amc", "nunique"), directs=("is_direct", lambda s: set(s.dropna())))
    assert (by_key["amcs"] == 1).all(), "a name key must never join two fund houses"
    assert (by_key["directs"].map(len) == 2).sum() > 500, "direct and regular lines of the same scheme should share one key"


def test_october_no_ter_scheme_is_left_without_a_master_key(world):
    ex, _, _, m = world
    if ex.sha256[:16].upper() != OCTOBER_SHA16:
        pytest.skip("not the October 2026 file")
    assert (m.unmatched.query("side == 'ter'")["reason"] == "no_name_key").sum() == 0


def test_october_active_coverage_does_not_regress(world):
    """Regression guard added after measuring the real October file (97.7 percent of active master schemes mapped); a drop means the matching rule got worse."""
    ex, _, master, m = world
    if ex.sha256[:16].upper() != OCTOBER_SHA16:
        pytest.skip("not the October 2026 file")
    act = _active(master)
    assert act["scheme_code"].isin(set(m.mapped["scheme_code"])).mean() >= 0.97
