"""NSE EOD adapters on the streaming foundation, driven by REAL response samples (tests/fixtures/nse). No network."""
from __future__ import annotations

import json
import urllib.error
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from qsde.stream.adapters import nse
from qsde.stream.adapters.nse import (HttpError, HttpFetcher, NseBhavcopyAdapter, NseCorporateActionsAdapter,
                                      NseSymbolChangeAdapter)
from qsde.stream.base import RawItem
from qsde.stream.health import CheckpointStore, InMemoryHealthStore, LogAlertSink
from qsde.stream.landing import LandingZone
from qsde.stream.runner import InMemorySink, replay, run_once

FIX = Path(__file__).parent / "fixtures" / "nse"
UTC = timezone.utc
# Monday 2026-10-19 13:30 UTC = 19:00 IST: the 16 Oct (Fri) file is published
NOW = datetime(2026, 10, 19, 13, 30, tzinfo=UTC)


class FakeFetcher:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def get(self, url, headers=None):
        self.calls.append(url)
        for k, v in self.routes.items():
            if k in url:
                if isinstance(v, Exception):
                    raise v
                return v
        raise HttpError(404, url)


@pytest.fixture
def env(tmp_path):
    return dict(landing=LandingZone(tmp_path / "landing"), sink=InMemorySink(), health=InMemoryHealthStore(),
                cp=CheckpointStore(tmp_path / "cp.json"), alerts=LogAlertSink())


def go(a, env, now=NOW):
    return run_once(a, env["landing"], env["sink"], env["health"], env["cp"], env["alerts"], clock=lambda: now)


BHAV = (FIX / "sec_bhavdata_full_16102026_sample.csv").read_bytes()
CA = (FIX / "corporate_actions_sample.json").read_bytes()


class TestBhavcopy:
    def adapter(self, routes=None):
        f = FakeFetcher(routes if routes is not None else {"16102026": BHAV})
        return NseBhavcopyAdapter(f, now=lambda: NOW, start_date=date(2026, 10, 16)), f

    def test_parses_the_real_layout_into_eod_raw_columns(self):
        a, _ = self.adapter()
        df = a.parse(RawItem(BHAV, NOW, None, {"url": "u", "session_date": "2026-10-16"}, "csv"))
        assert len(df) >= 5 and set(df["date"]) == {date(2026, 10, 16)}
        assert {"exchange", "symbol", "series", "open", "high", "low", "close", "prev_close", "volume", "deliv_per"} <= set(df.columns)
        r = df[df["symbol"] == "360ONE"].iloc[0]
        assert r.close == 1075.0 and r.prev_close == 1073.0 and r.deliv_per == 35.74 and r.exchange == "NSE"
        assert df["event_time"].iloc[0].hour == 10                                  # 15:30 IST = 10:00 UTC

    def test_holidays_404_are_skipped_not_failures(self):
        a, f = self.adapter({"16102026": BHAV})                 # other days return 404 from the fake
        a.start_date = date(2026, 10, 14)
        items = a.fetch(None)
        assert [i.meta["session_date"] for i in items] == ["2026-10-16"] and len(f.calls) == 4   # 14, 15, 16 and today (19:00 IST Monday): 404s are skipped

    def test_does_not_ask_for_today_before_the_file_is_published(self):
        early = datetime(2026, 10, 19, 6, 0, tzinfo=UTC)                           # 11:30 IST Monday
        a = NseBhavcopyAdapter(FakeFetcher({}), now=lambda: early, start_date=date(2026, 10, 19))
        assert a.fetch(None) == [] and a.fetcher.calls == []

    def test_server_error_is_a_failure_that_reaches_health(self, env):
        a, _ = self.adapter({"16102026": HttpError(503, "u")})
        r = go(a, env)
        assert not r.ok and env["health"].get("nse_bhavcopy").status == "failing"

    def test_end_to_end_lands_raw_then_loads_and_replays_identically(self, env):
        a, _ = self.adapter()
        r = go(a, env)
        assert r.ok and r.rows >= 5 and len(list(env["landing"].records("nse_bhavcopy"))) == 1
        again = go(a, env, NOW + timedelta(minutes=30))                            # checkpoint moved past it: nothing new
        assert again.rows == 0
        fresh = InMemorySink()
        rr = replay(NseBhavcopyAdapter(FakeFetcher({})), env["landing"], fresh)
        assert rr.rows == r.rows and len(fresh.frame("nse_bhavcopy")) == r.rows

    def test_corrupt_file_is_landed_and_flagged_not_loaded(self, env):
        a, _ = self.adapter({"16102026": b"<html>Access Denied</html>"})
        r = go(a, env)
        assert not r.ok and r.stage == "parse" and len(list(env["landing"].records("nse_bhavcopy"))) == 1
        assert len(env["sink"].frame("nse_bhavcopy")) == 0 and env["alerts"].sent[-1].severity == "critical"

    def test_known_at_is_the_fetch_time_live_and_publication_time_in_backfill(self, env):
        a, _ = self.adapter()
        go(a, env)
        df = env["sink"].frame("nse_bhavcopy")
        assert (pd.to_datetime(df["known_at"]) >= pd.to_datetime(df["event_time"])).all()
        assert (pd.to_datetime(df["known_at"]) == pd.Timestamp(NOW)).all()
        fresh = InMemorySink()
        replay(a, env["landing"], fresh, mode="backfill")
        k = pd.to_datetime(fresh.frame("nse_bhavcopy")["known_at"]).iloc[0]
        assert k == pd.Timestamp("2026-10-16T12:30:00Z")                           # 18:00 IST on the session date


class TestCorporateActions:
    def test_real_payload_yields_record_date_face_value_isin_and_categories(self):
        a = NseCorporateActionsAdapter(FakeFetcher({}), now=lambda: NOW)
        df = a.parse(RawItem(CA, NOW, None, {}, "json"))
        assert len(df) == 27 and a.validate(df) == []
        natco = df[df["symbol"] == "NATCOPHARM"].iloc[0]
        assert natco.category == "RIGHTS" and natco["isin"] == "INE987B01018" and natco.face_value == 2.0
        assert natco.ex_date == date(2026, 10, 1) and natco.record_date == date(2026, 10, 1)
        assert set(df["category"]) >= {"RIGHTS", "DIVIDEND", "DEMERGER"}
        # price factors only for parseable equity ratios; never invented for dividends / rights / demergers
        assert df[df["category"].isin(["DIVIDEND", "RIGHTS", "DEMERGER"])]["factor"].isna().all()
        split = df[df["category"] == "SPLIT_BONUS"]
        assert len(split) >= 1 and split["factor"].notna().all()

    def test_repeated_fetch_adds_nothing_and_a_correction_adds_a_row(self, env):
        f = FakeFetcher({"corporates-corporateActions": CA})
        clock = {"t": NOW}
        a = NseCorporateActionsAdapter(f, now=lambda: clock["t"])
        first = go(a, env)
        clock["t"] = NOW + timedelta(hours=2)
        second = go(a, env, clock["t"])
        assert first.rows == 27 and second.rows == 0
        rows = json.loads(CA)
        rows[0]["recDate"] = "05-Oct-2026"                                          # NSE corrects a record date
        f.routes["corporates-corporateActions"] = json.dumps(rows).encode()
        clock["t"] = NOW + timedelta(hours=4)
        third = go(a, env, clock["t"])
        assert third.rows == 1
        df = env["sink"].frame("nse_corporate_actions")
        assert len(df) == 28 and df["known_at"].nunique() == 2                      # first-seen time of old rows is preserved

    def test_non_list_payload_fails_loudly(self, env):
        a = NseCorporateActionsAdapter(FakeFetcher({"corporates": b'{"error":"blocked"}'}), now=lambda: NOW)
        r = go(a, env)
        assert not r.ok and r.stage == "parse" and env["health"].get("nse_corporate_actions").status == "parse_failed"

    def test_empty_window_is_valid(self):
        a = NseCorporateActionsAdapter(FakeFetcher({}), now=lambda: NOW)
        df = a.parse(RawItem(b"[]", NOW, None, {}, "json"))
        assert a.validate(df) == []


class TestSymbolChanges:
    FULL = Path(r"D:\Stoxsy\data\nse\symbolchange.csv")

    def test_short_file_is_rejected_as_a_broken_fetch(self):
        a = NseSymbolChangeAdapter(FakeFetcher({}), now=lambda: NOW)
        df = a.parse(RawItem((FIX / "symbolchange_sample.csv").read_bytes(), NOW, None, {}, "csv"))
        assert len(df) > 5 and any("only" in e for e in a.validate(df))

    @pytest.mark.skipif(not FULL.exists(), reason="stored NSE symbol-change file not present")
    def test_full_file_parses_and_validates(self):
        a = NseSymbolChangeAdapter(FakeFetcher({}), now=lambda: NOW)
        df = a.parse(RawItem(self.FULL.read_bytes(), NOW, None, {}, "csv"))
        assert a.validate(df) == [] and {"MINDAIND"} & set(df["old_symbol"])
        assert (df["change_type"] == "fund_series").sum() > 0


class TestHttpFetcher:
    class Resp:
        def __init__(self, body):
            self.body = body

        def read(self):
            return self.body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def test_404_is_not_retried(self, monkeypatch):
        n = []

        def fake(req, timeout=0):
            n.append(1)
            raise urllib.error.HTTPError(req.full_url, 404, "nf", {}, None)

        monkeypatch.setattr(nse.urllib.request, "urlopen", fake)
        with pytest.raises(HttpError) as e:
            HttpFetcher(sleep=lambda s: None, min_interval_s=0).get("http://x")
        assert e.value.status == 404 and len(n) == 1

    def test_503_is_retried_then_succeeds(self, monkeypatch):
        seq = [urllib.error.HTTPError("http://x", 503, "busy", {}, None), self.Resp(b"ok")]

        def fake(req, timeout=0):
            v = seq.pop(0)
            if isinstance(v, Exception):
                raise v
            return v

        monkeypatch.setattr(nse.urllib.request, "urlopen", fake)
        assert HttpFetcher(sleep=lambda s: None, min_interval_s=0).get("http://x") == b"ok"

    def test_persistent_failure_raises_after_the_retries(self, monkeypatch):
        n = []

        def fake(req, timeout=0):
            n.append(1)
            raise urllib.error.HTTPError(req.full_url, 500, "boom", {}, None)

        monkeypatch.setattr(nse.urllib.request, "urlopen", fake)
        with pytest.raises(HttpError):
            HttpFetcher(retries=3, sleep=lambda s: None, min_interval_s=0).get("http://x")
        assert len(n) == 3


class TestDatabaseSinks:
    """Real DB round trips with cleanup; skipped when the database is unavailable."""

    def _db(self):
        try:
            from qsde.db.connection import execute_sql, read_sql
            read_sql("SELECT 1 FROM nse_corporate_actions LIMIT 1")
            return execute_sql, read_sql
        except Exception as e:  # noqa: BLE001
            pytest.skip(f"database or migration 033 unavailable: {e}")

    def test_corporate_actions_sink_is_idempotent_and_keeps_first_seen_known_at(self):
        from qsde.stream.runner import stamp_known_at
        from qsde.stream.sinks import PgCorporateActionsSink

        execute_sql, read_sql = self._db()
        a = NseCorporateActionsAdapter(FakeFetcher({}), now=lambda: NOW)
        item = RawItem(CA, NOW, None, {}, "json")
        df = stamp_known_at(a.parse(item), item)
        df = df[df["symbol"] == "NATCOPHARM"].copy()
        df["symbol"] = "PYTESTCA"                                                  # never collide with real rows
        try:
            s = PgCorporateActionsSink()
            assert s.upsert("nse_corporate_actions", df, a.key_cols) == len(df)
            later = df.copy()
            later["known_at"] = later["known_at"] + pd.Timedelta(hours=3)
            assert s.upsert("nse_corporate_actions", later, a.key_cols) == 0       # same content: not a new fact
            row = read_sql("SELECT * FROM nse_corporate_actions WHERE symbol = 'PYTESTCA'").iloc[0]
            assert row["isin"] == "INE987B01018" and row["category"] == "RIGHTS" and pd.Timestamp(row["known_at"]) == pd.Timestamp(NOW)
        finally:
            execute_sql("DELETE FROM nse_corporate_actions WHERE symbol = 'PYTESTCA'")
