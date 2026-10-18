"""Parquet feature store: determinism, point-in-time reads, NaN preservation, registry.

Hermetic except the DB-marked class and the pilot-parity class (both skip cleanly).
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import numpy as np
import pandas as pd
import pytest

from qsde.features import store
from qsde.features.store import SnapshotWriter, known_at_utc, read_features, read_latest_rows

FIXED = datetime(2026, 10, 18, 12, 0, 0, tzinfo=timezone.utc)
FP = {"eod_n": 1, "note": "fixture"}


def _frame(n=80, syms=("AAA", "BBB"), seed=0, start="2025-11-03", nan_head=5) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(start, periods=n, name="date")
    parts = []
    for i, s in enumerate(syms):
        f = pd.DataFrame({"symbol": s,
                          "tech_obv_slope": rng.normal(size=n),
                          "tech_rsi_14": rng.uniform(20, 80, n),
                          "smc_bos": rng.integers(-1, 2, n).astype(float)}, index=idx)
        f.iloc[:nan_head, f.columns.get_loc("tech_obv_slope")] = np.nan        # warm-up NaN
        parts.append(f)
    return pd.concat(parts)


def _build(root, df=None, **kw):
    w = SnapshotWriter(kw.pop("factor_version", "ffd_v2"), kw.pop("universe_version", "uni_test"),
                       kind=kw.pop("kind", "full"), root=root, fingerprint=FP, sha="deadbeef",
                       built_at=kw.pop("built_at", FIXED), **kw)
    w.add(_frame() if df is None else df)
    return w.close(register=False)


@pytest.fixture(autouse=True)
def _no_db(monkeypatch):
    """Registry/DB paths are exercised in TestRegistryOnDb; elsewhere force the disk fallback."""
    monkeypatch.setattr(store, "_register", lambda s: False)
    import qsde.db.connection as c
    monkeypatch.setattr(c, "read_sql", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no db")))


class TestDeterminism:
    def test_two_builds_have_identical_hash_and_file_bytes(self, tmp_path):
        a = _build(tmp_path / "a")
        b = _build(tmp_path / "b")
        assert a.dataset_hash == b.dataset_hash and a.snapshot_id == b.snapshot_id
        for fa, fb in zip(a.files, b.files):
            assert fa.read_bytes() == fb.read_bytes()

    def test_row_order_of_the_input_does_not_matter(self, tmp_path):
        df = _frame()
        a = _build(tmp_path / "a", df)
        b = _build(tmp_path / "b", df.sample(frac=1.0, random_state=3))
        assert a.dataset_hash == b.dataset_hash

    def test_changed_value_changes_hash(self, tmp_path):
        df = _frame()
        a = _build(tmp_path / "a", df)
        df2 = df.copy()
        df2.iloc[10, df2.columns.get_loc("tech_rsi_14")] += 1e-6
        assert _build(tmp_path / "b", df2).dataset_hash != a.dataset_hash

    def test_changed_input_fingerprint_changes_hash(self, tmp_path):
        a = _build(tmp_path / "a")
        w = SnapshotWriter("ffd_v2", "uni_test", root=tmp_path / "b", fingerprint={"eod_n": 2},
                           sha="deadbeef", built_at=FIXED)
        w.add(_frame())
        assert w.close(register=False).dataset_hash != a.dataset_hash

    def test_no_pandas_metadata_in_files(self, tmp_path):
        import pyarrow.parquet as pq
        s = _build(tmp_path)
        md = pq.read_schema(s.files[0]).metadata or {}
        assert b"pandas" not in md


class TestReader:
    def test_roundtrip_values_and_nan_are_preserved(self, tmp_path):
        df = _frame()
        s = _build(tmp_path, df)
        got = read_features("ffd_v2", root=tmp_path).set_index(["symbol", "as_of_date"]).sort_index()
        ref = df.reset_index().rename(columns={"date": "as_of_date"})
        ref["as_of_date"] = ref["as_of_date"].dt.date
        ref = ref.set_index(["symbol", "as_of_date"]).sort_index()
        np.testing.assert_array_equal(got["tech_rsi_14"].to_numpy(), ref["tech_rsi_14"].to_numpy())
        assert got["tech_obv_slope"].isna().sum() == 2 * 5               # warm-up stays NaN
        assert s.n_rows == len(df)

    def test_filters_symbols_dates_columns(self, tmp_path):
        _build(tmp_path)
        d0, d1 = date(2025, 12, 1), date(2025, 12, 31)
        got = read_features("ffd_v2", symbols=["AAA"], start=d0, end=d1, columns=["tech_rsi_14"], root=tmp_path)
        assert set(got["symbol"]) == {"AAA"} and got["as_of_date"].min() >= d0 and got["as_of_date"].max() <= d1
        assert list(got.columns) == ["symbol", "as_of_date", "known_at", "tech_rsi_14"]

    def test_known_before_is_a_strict_point_in_time_cut(self, tmp_path):
        _build(tmp_path)
        cut = pd.Timestamp("2025-12-10 12:29:59", tz="UTC")        # 1s before 2025-12-10 becomes knowable
        got = read_features("ffd_v2", known_before=cut, root=tmp_path)
        assert got["as_of_date"].max() == date(2025, 12, 9)
        got2 = read_features("ffd_v2", known_before=pd.Timestamp("2025-12-10 12:30:00", tz="UTC"), root=tmp_path)
        assert got2["as_of_date"].max() == date(2025, 12, 10)

    def test_known_at_rule_is_session_date_1800_ist(self):
        k = known_at_utc(pd.Series(pd.to_datetime(["2025-12-10"])))
        assert k[0] == pd.Timestamp("2025-12-10 12:30:00")

    def test_increment_overrides_overlap_and_adds_new_dates(self, tmp_path):
        base = _frame()
        _build(tmp_path, base)
        inc = base.copy()
        inc = inc[inc["symbol"] == "AAA"].iloc[-5:].copy()
        inc["tech_rsi_14"] = 999.0
        last = inc.index.max()
        extra = pd.DataFrame({"symbol": "AAA", "tech_obv_slope": 1.0, "tech_rsi_14": 55.0, "smc_bos": 0.0},
                             index=pd.DatetimeIndex([last + pd.offsets.BDay(1)], name="date"))
        w = SnapshotWriter("ffd_v2", "uni_test", kind="increment", root=tmp_path, fingerprint=FP,
                           sha="x", built_at=datetime(2026, 10, 19, tzinfo=timezone.utc))
        w.add(pd.concat([inc, extra]))
        w.close(register=False)
        got = read_features("ffd_v2", root=tmp_path)
        a = got[got["symbol"] == "AAA"].set_index("as_of_date")
        assert (a["tech_rsi_14"].iloc[-6:-1] == 999.0).all() and a["tech_rsi_14"].iloc[-1] == 55.0
        assert len(got) == len(base) + 1

    def test_full_snapshot_supersedes_earlier_ones_on_disk_fallback(self, tmp_path):
        _build(tmp_path, built_at=datetime(2026, 10, 1, tzinfo=timezone.utc))
        new = _frame(syms=("ZZZ",))
        _build(tmp_path, new, built_at=datetime(2026, 10, 2, tzinfo=timezone.utc))
        assert set(read_features("ffd_v2", root=tmp_path)["symbol"]) == {"ZZZ"}

    def test_pinned_snapshot_ids_read_exactly_that_snapshot(self, tmp_path):
        s1 = _build(tmp_path, built_at=datetime(2026, 10, 1, tzinfo=timezone.utc))
        _build(tmp_path, _frame(syms=("ZZZ",)), built_at=datetime(2026, 10, 2, tzinfo=timezone.utc))
        got = read_features("ffd_v2", snapshot_ids=[s1.snapshot_id], root=tmp_path)
        assert set(got["symbol"]) == {"AAA", "BBB"}

    def test_read_latest_rows_takes_one_complete_date_per_symbol(self, tmp_path):
        df = _frame()
        _build(tmp_path, df)
        # make BBB's final day lack a value: the row must stay NaN, NOT be stitched from an earlier day
        df2 = df.copy()
        bbb_last = df2[df2["symbol"] == "BBB"].index.max()
        df2.loc[(df2.index == bbb_last) & (df2["symbol"] == "BBB"), "tech_rsi_14"] = np.nan
        _build(tmp_path / "x", df2)
        got = read_latest_rows(max_age_days=400, asof=bbb_last, root=tmp_path / "x")
        row = got[got["symbol"] == "BBB"].iloc[0]
        assert row["as_of_date"] == bbb_last.date() and np.isnan(row["tech_rsi_14"])
        assert len(got) == 2

    def test_latest_factor_series(self, tmp_path, monkeypatch):
        df = _frame(start=str((pd.Timestamp.today() - pd.Timedelta(days=60)).date()))
        _build(tmp_path, df)
        s = store.latest_factor("tech_rsi_14", root=tmp_path)
        assert set(s.index) == {"AAA", "BBB"}


class TestValidation:
    def test_non_numeric_feature_rejected(self, tmp_path):
        bad = _frame()
        bad["tech_oops"] = "x"
        w = SnapshotWriter("ffd_v2", "u", root=tmp_path, fingerprint=FP, sha="s", built_at=FIXED)
        with pytest.raises(ValueError, match="non-numeric"):
            w.add(bad)

    def test_empty_writer_raises(self, tmp_path):
        with pytest.raises(ValueError):
            SnapshotWriter("ffd_v2", "u", root=tmp_path).close(register=False)

    def test_existing_snapshot_is_never_overwritten(self, tmp_path):
        _build(tmp_path)
        with pytest.raises(FileExistsError):
            _build(tmp_path)

    def test_manifest_declares_policies_and_hash_matches_files(self, tmp_path):
        s = _build(tmp_path)
        m = s.manifest
        pol = m["corporate_action_policy"]
        assert m["nan_policy"].startswith("preserved") and pol["method"] == "source_confirmed_v2"
        assert "SOURCE_UNCONFIRMED" in pol["label_exclusions"] and "price_inferred" in " ".join(pol["adjusted"])
        assert "recent_gap_symbols_from_db" in pol["serving"]
        assert m["params"]["fracdiff_auto_widen"] is False and m["files"][0]["sha256"]
        assert sum(f["rows"] for f in m["files"]) == m["rows"]


def _db_up() -> bool:
    try:
        from qsde.db import connection
        import importlib
        importlib.reload(connection)
        connection.read_sql("SELECT 1 FROM feature_snapshots LIMIT 1")
        return True
    except Exception:  # noqa: BLE001
        return False


@pytest.mark.skipif(not _db_up(), reason="Postgres with feature_snapshots not reachable")
class TestRegistryOnDb:
    VER = "test_fv_registry"

    def _clean(self):
        from qsde.db.connection import execute_sql
        execute_sql("DELETE FROM feature_snapshots WHERE factor_version = %s", (self.VER,))

    def test_register_full_supersedes_and_increment_adds(self, tmp_path, monkeypatch):
        monkeypatch.undo()                               # real _register / real DB for this test
        from qsde.db.connection import read_sql
        self._clean()
        try:
            def build(kind, day):
                w = SnapshotWriter(self.VER, "uni_a", kind=kind, root=tmp_path, fingerprint=FP, sha="s",
                                   built_at=datetime(2026, 10, day, tzinfo=timezone.utc))
                w.add(_frame(syms=("S%d" % day,)))
                return w.close(register=True)

            a = build("full", 1)
            b = build("increment", 2)
            c = build("full", 3)                         # supersedes a and b
            cur = read_sql("SELECT snapshot_id, is_current FROM feature_snapshots "
                           "WHERE factor_version = :v ORDER BY built_at", params={"v": self.VER})
            assert dict(zip(cur["snapshot_id"], cur["is_current"])) == {
                a.snapshot_id: False, b.snapshot_id: False, c.snapshot_id: True}
            row = read_sql("SELECT dataset_hash, n_rows, manifest FROM feature_snapshots "
                           "WHERE snapshot_id = :i", params={"i": c.snapshot_id}).iloc[0]
            assert row["dataset_hash"] == c.dataset_hash and row["n_rows"] == c.n_rows
        finally:
            self._clean()


@pytest.mark.skipif(not _db_up(), reason="Postgres not reachable")
class TestPilotParityWithLongForm:
    """The Parquet pilot snapshot must equal the old ffd_v2 factor_pit rows exactly.
    Skips when the pilot rows or snapshot are not present (e.g. after factor_pit cleanup)."""

    def test_parquet_equals_factor_pit_for_sample_symbols(self, monkeypatch):
        import os
        if os.getenv("STOXSY_RUN_PILOT_PARITY") != "1":
            pytest.skip("one-off parity check done 2026-10-18 (8,195,321 cells equal). The legacy factor_pit "
                        "pilot rows predate the source-confirmed corporate-action rebuild, so they no longer "
                        "match a fresh snapshot by design; set STOXSY_RUN_PILOT_PARITY=1 only against a "
                        "snapshot built from the same inputs.")
        monkeypatch.undo()
        from qsde.db.connection import read_sql
        have = read_sql("SELECT count(*) n FROM factor_pit WHERE factor_version = 'ffd_v2'").iloc[0]["n"]
        snaps = store.current_snapshots("ffd_v2")
        if have == 0 or not snaps:
            pytest.skip("no ffd_v2 factor_pit rows or no current snapshot")
        sample = ["RELIANCE", "TCS", "HDFCBANK"]
        lf = read_sql("SELECT symbol, as_of_date, factor_name, factor_value FROM factor_pit "
                      "WHERE factor_version = 'ffd_v2' AND valid_to = 'infinity' AND symbol = ANY(:s)",
                      params={"s": sample})
        if lf.empty:
            pytest.skip("sample symbols not in the pilot")
        wide = read_features("ffd_v2", symbols=sample)
        feats = [c for c in wide.columns if c not in ("symbol", "as_of_date", "known_at")]
        long = wide.melt(id_vars=["symbol", "as_of_date"], value_vars=feats, var_name="factor_name",
                         value_name="v").dropna(subset=["v"])
        m = long.merge(lf, on=["symbol", "as_of_date", "factor_name"], how="outer", indicator=True)
        assert (m["_merge"] == "both").all(), m["_merge"].value_counts().to_dict()
        assert (m["v"] == m["factor_value"]).all()
