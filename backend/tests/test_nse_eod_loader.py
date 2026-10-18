"""Acceptance tests for the NSE bhavcopy loader (eod_raw).

Hermetic except the two marked classes:
  * TestLoaderIdempotentOnDb   needs the Postgres container (skips if down)
  * TestAgainstRealArchive     needs the local archive (skips if absent)
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from qsde.ingestion import nse_eod
from qsde.ingestion.nse_eod import (
    normalize_legacy_frame, pick_primary_series, validate_eod,
)


def _raw(rows: list[dict] | None = None, n: int = 3, d: str = "2026-04-02") -> pd.DataFrame:
    base = [
        dict(SYMBOL="AAA", SERIES="EQ", DATE1=pd.Timestamp(d), PREV_CLOSE=100.0, OPEN_PRICE=101.0,
             HIGH_PRICE=105.0, LOW_PRICE=99.0, LAST_PRICE="103.0", CLOSE_PRICE=104.0, AVG_PRICE=102.0,
             TTL_TRD_QNTY=1000, TURNOVER_LACS=10.2, NO_OF_TRADES=50, DELIV_QTY="400", DELIV_PER="40.0"),
        dict(SYMBOL=" BBB ", SERIES="be", DATE1=pd.Timestamp(d), PREV_CLOSE=50.0, OPEN_PRICE=50.0,
             HIGH_PRICE=52.0, LOW_PRICE=49.0, LAST_PRICE="51", CLOSE_PRICE=51.0, AVG_PRICE=50.5,
             TTL_TRD_QNTY=2000, TURNOVER_LACS=10.1, NO_OF_TRADES=70, DELIV_QTY=" ", DELIV_PER=" "),
        dict(SYMBOL="GOVT", SERIES="GS", DATE1=pd.Timestamp(d), PREV_CLOSE=100.0, OPEN_PRICE=100.0,
             HIGH_PRICE=100.0, LOW_PRICE=100.0, LAST_PRICE="100", CLOSE_PRICE=100.0, AVG_PRICE=100.0,
             TTL_TRD_QNTY=10, TURNOVER_LACS=0.1, NO_OF_TRADES=1, DELIV_QTY="-", DELIV_PER="-"),
    ]
    return pd.DataFrame(rows if rows is not None else base)


FILE_DATE = date(2026, 4, 2)


class TestNormalize:
    def test_equity_series_only_and_placeholders_become_nan(self):
        out = normalize_legacy_frame(_raw(), FILE_DATE, "f.parquet")
        assert sorted(out["series"]) == ["BE", "EQ"]                  # GS dropped, 'be' upper-cased
        assert out.loc[out["symbol"] == "BBB", "deliv_per"].isna().all()   # ' ' -> NaN
        assert out.loc[out["symbol"] == "BBB", "symbol"].shape[0] == 1     # ' BBB ' stripped
        assert out["last"].dtype.kind == "f"                              # '103.0' coerced
        assert (out["exchange"] == "NSE").all()

    def test_inner_date_is_authoritative_over_file_name(self):
        # holiday file 2026-04-03 holding the 2026-04-02 session
        out = normalize_legacy_frame(_raw(d="2026-04-02"), date(2026, 4, 3), "2026-04-03.parquet")
        assert set(out["date"]) == {date(2026, 4, 2)}
        assert out["date_mismatch"].all()

    def test_unparseable_inner_date_falls_back_to_file_date(self):
        raw = _raw()
        raw["DATE1"] = "not-a-date"
        out = normalize_legacy_frame(raw, FILE_DATE, "f.parquet")
        assert set(out["date"]) == {FILE_DATE} and out["date_mismatch"].all()

    def test_missing_isin_and_delivery_columns_are_tolerated(self):
        raw = _raw().drop(columns=["DELIV_QTY", "DELIV_PER"])
        out = normalize_legacy_frame(raw, FILE_DATE, "f.parquet")
        assert out["deliv_per"].isna().all() and "isin" in out.columns


class TestValidate:
    @staticmethod
    def _one(**over):
        row = dict(exchange="NSE", symbol="AAA", series="EQ", date=FILE_DATE, isin=None, open=10.0,
                   high=11.0, low=9.0, close=10.5, last=10.5, prev_close=10.0, volume=100,
                   turnover_lacs=1.0, trades=5, deliv_qty=40, deliv_per=40.0, source_file="f",
                   date_mismatch=False)
        row.update(over)
        return pd.DataFrame([row])

    @pytest.mark.parametrize("over,reason", [
        (dict(high=10.0, close=10.5), "high_below_open_or_close"),
        (dict(low=10.2, open=10.0), "low_above_open_or_close"),
        (dict(open=0.0, low=0.0), "bad_open"),
        (dict(close=-1.0), "bad_close"),
        (dict(volume=-5), "bad_volume"),
        (dict(symbol=""), "missing_symbol"),
        (dict(open=np.nan), "bad_open"),
    ])
    def test_hard_rules_reject_with_reason(self, over, reason):
        res = validate_eod(self._one(**over))
        assert len(res.valid) == 0 and res.rejects["reason"].iloc[0] == reason

    def test_clean_row_passes(self):
        res = validate_eod(self._one())
        assert len(res.valid) == 1 and res.counts["rows_rejected"] == 0

    def test_duplicate_keys_second_is_rejected(self):
        df = pd.concat([self._one(), self._one(close=10.4)], ignore_index=True)
        res = validate_eod(df)
        assert len(res.valid) == 1 and res.counts["rejects_by_reason"] == {"duplicate_key": 1}

    @pytest.mark.parametrize("v", [-1.0, 100.5, 250.0])
    def test_delivery_pct_out_of_range_is_nulled_not_rejected(self, v):
        res = validate_eod(self._one(deliv_per=v))
        assert len(res.valid) == 1 and np.isnan(res.valid["deliv_per"].iloc[0])
        assert res.counts["deliv_per_out_of_range_nulled"] == 1

    @pytest.mark.parametrize("v", [0.0, 37.2, 100.0])
    def test_delivery_pct_in_range_kept(self, v):
        assert validate_eod(self._one(deliv_per=v)).valid["deliv_per"].iloc[0] == v

    def test_counts_reconcile(self):
        df = pd.concat([self._one(), self._one(symbol="B", high=1.0), self._one(symbol="C")],
                       ignore_index=True)
        c = validate_eod(df).counts
        assert c["rows_in"] == c["rows_valid"] + c["rows_rejected"] == 3


class TestSeriesPriority:
    def test_eq_beats_be_on_same_day(self):
        df = pd.DataFrame({"symbol": ["X", "X", "Y"], "series": ["BE", "EQ", "BZ"],
                           "date": [FILE_DATE] * 3, "close": [1.0, 2.0, 3.0]})
        out = pick_primary_series(df).set_index("symbol")
        assert out.loc["X", "series"] == "EQ" and out.loc["X", "close"] == 2.0
        assert out.loc["Y", "series"] == "BZ" and len(out) == 2


class TestFileDiscovery:
    def test_layouts_and_range(self, tmp_path):
        for sub, names in {"old_legacy/parsed": ["2020-06-29", "2020-06-30", "2019-01-01"],
                           "legacy/parsed": ["2020-07-01", "2020-07-02", "bad-name"]}.items():
            (tmp_path / sub).mkdir(parents=True)
            for n in names:
                (tmp_path / sub / f"{n}.parquet").write_bytes(b"")
        got = nse_eod.list_files(date(2020, 6, 30), date(2020, 7, 1), tmp_path)
        assert [d.isoformat() for d, _ in got] == ["2020-06-30", "2020-07-01"]


def _db_up() -> bool:
    try:
        from qsde.db.connection import read_sql
        read_sql("SELECT 1 FROM eod_raw LIMIT 1")
        return True
    except Exception:  # noqa: BLE001
        return False


@pytest.mark.skipif(not _db_up(), reason="Postgres with eod_raw not reachable")
class TestLoaderIdempotentOnDb:
    SYMS = ("ZZTEST1", "ZZTEST2")

    def _clean(self):
        from qsde.db.connection import execute_sql
        execute_sql("DELETE FROM eod_raw WHERE symbol = ANY(%s)", (list(self.SYMS),))

    def test_reload_does_not_duplicate_and_nan_becomes_null(self):
        from qsde.db.connection import read_sql
        self._clean()
        try:
            raw = _raw([
                dict(SYMBOL="ZZTEST1", SERIES="EQ", DATE1=pd.Timestamp("2026-04-02"), PREV_CLOSE=10.0,
                     OPEN_PRICE=10.0, HIGH_PRICE=11.0, LOW_PRICE=9.5, LAST_PRICE="10.5",
                     CLOSE_PRICE=10.4, AVG_PRICE=10.2, TTL_TRD_QNTY=1234, TURNOVER_LACS=1.2,
                     NO_OF_TRADES=9, DELIV_QTY="500", DELIV_PER="40.5"),
                dict(SYMBOL="ZZTEST2", SERIES="EQ", DATE1=pd.Timestamp("2026-04-02"), PREV_CLOSE=20.0,
                     OPEN_PRICE=20.0, HIGH_PRICE=21.0, LOW_PRICE=19.5, LAST_PRICE="20.5",
                     CLOSE_PRICE=20.4, AVG_PRICE=20.2, TTL_TRD_QNTY=999, TURNOVER_LACS=2.0,
                     NO_OF_TRADES=8, DELIV_QTY=" ", DELIV_PER=" "),
            ])
            res = validate_eod(normalize_legacy_frame(raw, date(2026, 4, 2), "t.parquet"))
            n1 = nse_eod.upsert_eod_raw(res.valid)
            n2 = nse_eod.upsert_eod_raw(res.valid)      # second run: same rows, no duplicates
            assert n1 == n2 == 2
            got = read_sql("SELECT * FROM eod_raw WHERE symbol IN ('ZZTEST1','ZZTEST2') ORDER BY symbol")
            assert len(got) == 2
            assert got.loc[0, "volume"] == 1234 and got.loc[0, "deliv_per"] == 40.5
            assert pd.isna(got.loc[1, "deliv_per"]) and pd.isna(got.loc[1, "deliv_qty"])   # NULL, not NaN-value
        finally:
            self._clean()


ARCHIVE = nse_eod.DEFAULT_BASE


@pytest.mark.skipif(not (ARCHIVE / "legacy" / "parsed").exists(), reason="local archive not present")
class TestAgainstRealArchive:
    def test_validation_slice_is_clean_and_counts_reconcile(self):
        rep = nse_eod.load_range(date(2026, 4, 6), date(2026, 4, 10), dry_run=True)
        assert rep.files == 5 and rep.rows_in > 5 * 2000
        assert rep.rows_in == rep.rows_valid + rep.rows_rejected
        assert rep.rows_rejected / rep.rows_in < 0.005

    def test_holiday_files_are_flagged_stale(self):
        rep = nse_eod.load_range(date(2026, 4, 3), date(2026, 4, 3), dry_run=True)
        assert rep.stale_files and rep.stale_files[0]["data_date"] == "2026-04-02"

    def test_legacy_close_matches_independent_unified_bhavcopy(self):
        """Second NSE format (unified, 2024+) must agree on closes for the same day."""
        import pyarrow.parquet as pq

        d = date(2026, 4, 9)
        legacy = normalize_legacy_frame(pd.read_parquet(ARCHIVE / "legacy" / "parsed" / f"{d}.parquet"),
                                        d, "x")
        uni = pq.read_table(ARCHIVE / "unified" / "parsed" / f"{d}.parquet").to_pandas()
        uni = uni[(uni["Sgmt"] == "CM") & (uni["SctySrs"] == "EQ")][["TckrSymb", "ClsPric"]]
        m = legacy[legacy["series"] == "EQ"].merge(uni, left_on="symbol", right_on="TckrSymb")
        assert len(m) > 1500
        assert np.allclose(m["close"], m["ClsPric"], rtol=1e-6, atol=1e-6)
