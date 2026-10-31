"""Nightly feature refresh (qsde/features/refresh.py). The parity tests read the REAL feature snapshots and prices (`real_data`, local only); the rest is plain logic."""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qsde.features import refresh as R  # noqa: E402
from qsde.features import store  # noqa: E402

VERSIONS = ["ffd_v2s", "ffd_v2"]


def test_features_status_counts_price_sessions_after_the_newest_feature_day():
    sessions = [date(2026, 10, d) for d in (19, 20, 21, 22, 23, 26, 27, 28, 29, 30)]
    assert R.features_status(date(2026, 10, 16), sessions) == ("warn", 10)
    assert R.features_status(date(2026, 10, 28), sessions) == ("ok", 2)                  # within the tolerance: the routine runs after the close
    assert R.features_status(date(2026, 10, 27), sessions)[0] == "warn"                   # three sessions behind
    assert R.features_status(date(2026, 10, 30), sessions) == ("ok", 0)
    assert R.features_status(None, sessions) == ("warn", None)                            # nothing built yet


def test_refresh_refuses_to_start_without_a_full_snapshot_to_extend(tmp_path):
    with pytest.raises(ValueError, match="full snapshot"):
        R.refresh("ffd_v2", root=tmp_path, base_root=tmp_path)


@pytest.mark.real_data
@pytest.mark.parametrize("version", VERSIONS)
def test_an_increment_equals_the_full_snapshot_for_the_same_days(version, tmp_path):
    """The failure that retired ffd_v1: a daily tail computed differently from the backfill. Recompute three days that the full snapshot already holds and require identical values."""
    full = store.read_features(version)
    if full.empty:
        pytest.skip("no feature snapshot")
    full["as_of_date"] = pd.to_datetime(full["as_of_date"])
    days = sorted(full["as_of_date"].unique())[-3:]
    d1, d3 = pd.Timestamp(days[0]).date(), pd.Timestamp(days[-1]).date()
    syms = sorted(full.loc[full["as_of_date"] == days[-1], "symbol"].unique())[:25]
    res = R.refresh(version, since=d1, until=d3, symbols=syms, root=tmp_path, register=False)
    assert res["status"] == "written" and res["rows"] > 0
    new = store.read_features(version, root=tmp_path)
    new["as_of_date"] = pd.to_datetime(new["as_of_date"])
    old = full[full["symbol"].isin(syms) & (full["as_of_date"] >= pd.Timestamp(d1)) & (full["as_of_date"] <= pd.Timestamp(d3))]
    key = ["symbol", "as_of_date"]
    m = old.merge(new, on=key, suffixes=("_old", "_new"))
    assert len(m) == len(old) == len(new) > 0
    cols = [c for c in store.feature_columns(version) if f"{c}_old" in m.columns]
    assert len(cols) > 20
    for c in cols:
        a, b = m[f"{c}_old"].to_numpy(float), m[f"{c}_new"].to_numpy(float)
        assert np.allclose(a, b, rtol=1e-9, atol=1e-12, equal_nan=True), c


@pytest.mark.real_data
def test_a_dry_run_reports_what_it_would_add_and_writes_nothing():
    snaps_before = {v: len(store.all_snapshots(v)) for v in VERSIONS}
    for v in VERSIONS:
        res = R.refresh(v, dry_run=True)
        assert res["status"] in ("pending", "up_to_date") and "latest_feature_date" in res
        if res["status"] == "pending":
            assert res["days"] >= 1 and res["symbol_days"] > 0 and res["since"] > res["latest_feature_date"]
    assert {v: len(store.all_snapshots(v)) for v in VERSIONS} == snaps_before


@pytest.mark.real_data
def test_the_doctor_check_names_the_newest_feature_day_and_the_lag():
    from qsde.ops import checks as C

    c = C.check_features()
    assert c.level in ("ok", "warn") and c.name == "feature snapshots"
    assert "latest" in c.message and "session" in c.message
