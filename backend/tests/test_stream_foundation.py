"""Continuous-data foundation: raw-first, point-in-time, idempotent, replayable, visible failures. No network anywhere."""
from __future__ import annotations

import io
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import pytest

from qsde.stream.base import RawItem, SourceAdapter
from qsde.stream.health import (Alert, CheckpointStore, InMemoryHealthStore, JsonHealthStore, LogAlertSink)
from qsde.stream.landing import LandingZone
from qsde.stream.runner import InMemorySink, compute_known_at, replay, run_once, stamp_known_at
from qsde.stream.scheduler import due, evaluate_staleness, market_open, retry_delay, run_cycle

UTC = timezone.utc
T0 = datetime(2026, 10, 19, 5, 0, tzinfo=UTC)          # Monday 10:30 IST: market open


def csv_item(rows, fetched_at, published_at=None):
    df = pd.DataFrame(rows, columns=["symbol", "event_time", "value"])
    return RawItem(df.to_csv(index=False).encode(), fetched_at, published_at, {"url": "fixture"}, "csv")


class Fixture(SourceAdapter):
    name, key_cols, slo_seconds, interval_seconds = "fixture", ("symbol", "event_time"), 600, 60

    def __init__(self, items=None, fail=None):
        self.items, self.fail, self.calls = list(items or []), fail, 0

    def fetch(self, since):
        self.calls += 1
        if self.fail:
            raise self.fail
        return [i for i in self.items if since is None or i.fetched_at > since]

    def parse(self, item):
        df = pd.read_csv(io.BytesIO(item.payload))
        df["event_time"] = pd.to_datetime(df["event_time"], utc=True)
        return df


@pytest.fixture
def env(tmp_path):
    return dict(landing=LandingZone(tmp_path / "landing"), sink=InMemorySink(), health=InMemoryHealthStore(),
                cp=CheckpointStore(tmp_path / "cp.json"), alerts=LogAlertSink(tmp_path / "alerts.jsonl"))


def go(a, env, now=T0):
    return run_once(a, env["landing"], env["sink"], env["health"], env["cp"], env["alerts"], clock=lambda: now)


ROW = [("AAA", "2026-10-19T04:59:00Z", 1.0)]


class TestRawFirst:
    def test_bytes_are_landed_even_when_parsing_fails(self, env):
        bad = RawItem(b"not,a,valid\nfile", T0, None, {}, "csv")

        class Strict(Fixture):
            def parse(self, item):
                raise ValueError("schema changed")

        a = Strict([bad])
        r = go(a, env)
        recs = list(env["landing"].records("fixture"))
        assert not r.ok and r.stage == "parse" and len(recs) == 1 and recs[0].path.read_bytes() == bad.payload
        assert env["health"].get("fixture").status == "parse_failed" and env["alerts"].sent[-1].severity == "critical"

    def test_landing_is_immutable_and_atomic(self, env):
        item = csv_item(ROW, T0)
        r1 = env["landing"].write("fixture", item)
        r2 = env["landing"].write("fixture", item)                        # same bytes again: no-op
        assert r1.path == r2.path and len(list((r1.path.parent).glob("*.csv"))) == 1
        assert not list(r1.path.parent.glob("*.part"))
        r1.path.write_bytes(b"tampered")
        with pytest.raises(ValueError):
            env["landing"].read(r1)                                       # a changed file is detected, never trusted


class TestPointInTime:
    def test_live_known_at_is_never_before_the_fetch(self):
        e = pd.Timestamp("2026-10-19T04:00:00Z")
        assert compute_known_at(e, None, T0) == T0
        assert compute_known_at(e, T0 + timedelta(minutes=5), T0) == T0 + timedelta(minutes=5)     # publisher later wins

    def test_backfill_uses_publisher_or_event_time_plus_lag_not_the_late_fetch(self):
        e = pd.Timestamp("2020-01-02T10:00:00Z")
        k = compute_known_at(e, None, T0, mode="backfill", backfill_lag=timedelta(hours=2))
        assert k == datetime(2020, 1, 2, 12, 0, tzinfo=UTC) and k < T0

    def test_known_at_never_precedes_the_event(self):
        item = csv_item([("AAA", "2026-10-19T09:00:00Z", 1.0)], T0)                 # claims to describe the future
        out = stamp_known_at(pd.read_csv(io.BytesIO(item.payload)).assign(event_time=lambda d: pd.to_datetime(d.event_time, utc=True)), item)
        assert (out["known_at"] >= out["event_time"]).all()


class TestIdempotenceAndCorrections:
    def test_second_run_over_the_same_fetch_adds_nothing(self, env):
        a = Fixture([csv_item(ROW, T0)])
        r1, r2 = go(a, env), go(a, env, T0 + timedelta(minutes=2))
        assert r1.rows == 1 and r2.rows == 0 and len(env["sink"].frame("fixture")) == 1

    def test_correction_is_a_new_row_with_a_later_known_at(self, env):
        a = Fixture([csv_item(ROW, T0), csv_item([("AAA", "2026-10-19T04:59:00Z", 1.5)], T0 + timedelta(hours=1))])
        go(a, env)
        df = env["sink"].frame("fixture").sort_values("known_at")
        assert len(df) == 2 and df["known_at"].is_monotonic_increasing and list(df["value"]) == [1.0, 1.5]

    def test_checkpoint_advances_only_after_a_successful_commit(self, env):
        class Boom(InMemorySink):
            def upsert(self, *a, **k):
                raise RuntimeError("db down")

        a = Fixture([csv_item(ROW, T0)])
        with pytest.raises(RuntimeError):
            run_once(a, env["landing"], Boom(), env["health"], env["cp"], env["alerts"], clock=lambda: T0)
        assert env["cp"].get("fixture") is None                           # nothing committed -> nothing skipped next time
        assert len(list(env["landing"].records("fixture"))) == 1          # but the bytes are safe


class TestReplay:
    def test_replay_from_landing_reproduces_the_live_rows_without_the_network(self, env, tmp_path):
        a = Fixture([csv_item(ROW, T0), csv_item([("BBB", "2026-10-19T05:00:00Z", 2.0)], T0 + timedelta(minutes=1))])
        go(a, env)
        live = env["sink"].frame("fixture").sort_values(["symbol", "known_at"]).reset_index(drop=True)
        fresh = InMemorySink()
        offline = Fixture(fail=RuntimeError("network must not be touched"))
        r = replay(offline, env["landing"], fresh)
        again = fresh.frame("fixture").sort_values(["symbol", "known_at"]).reset_index(drop=True)
        assert r.ok and offline.calls == 0
        pd.testing.assert_frame_equal(live, again, check_like=True)

    def test_replay_after_a_parser_fix_recovers_previously_failed_items(self, env):
        item = csv_item(ROW, T0)

        class Broken(Fixture):
            def parse(self, i):
                raise ValueError("bug")

        go(Broken([item]), env)
        assert len(env["sink"].frame("fixture")) == 0
        r = replay(Fixture(), env["landing"], env["sink"])
        assert r.rows == 1 and len(env["sink"].frame("fixture")) == 1


class TestFailureVisibility:
    def test_fetch_failures_update_health_and_escalate(self, env):
        a = Fixture(fail=ConnectionError("dns"))
        for i in range(3):
            r = go(a, env, T0 + timedelta(minutes=10 * i))
        h = env["health"].get("fixture")
        assert not r.ok and h.consecutive_failures == 3 and h.status == "failing"
        assert [x.severity for x in env["alerts"].sent] == ["warning"] or env["alerts"].sent[-1].severity == "critical"

    def test_success_resets_failure_state(self, env):
        a = Fixture(fail=ConnectionError("x"))
        go(a, env)
        a.fail, a.items = None, [csv_item(ROW, T0 + timedelta(minutes=5))]
        go(a, env, T0 + timedelta(minutes=10))
        h = env["health"].get("fixture")
        assert h.status == "ok" and h.consecutive_failures == 0 and h.last_error is None

    def test_alert_dedup_inside_cooldown(self, tmp_path):
        s = LogAlertSink(cooldown_s=900)
        assert s.emit(Alert("warning", "t", "b", "k", T0)) and not s.emit(Alert("warning", "t", "b", "k", T0 + timedelta(minutes=5)))
        assert s.emit(Alert("warning", "t", "b", "k", T0 + timedelta(minutes=20)))

    def test_json_health_store_roundtrips(self, tmp_path):
        s = JsonHealthStore(tmp_path / "h.json")
        h = s.get("x")
        h.last_success_at, h.status, h.consecutive_failures = T0, "ok", 2
        s.put(h)
        h2 = JsonHealthStore(tmp_path / "h.json").get("x")
        assert h2.last_success_at == T0 and h2.status == "ok" and h2.consecutive_failures == 2


class TestScheduling:
    def test_market_hours_and_holidays(self):
        assert market_open(T0)                                              # Mon 10:30 IST
        assert not market_open(datetime(2026, 10, 19, 11, 0, tzinfo=UTC))   # 16:30 IST
        assert not market_open(datetime(2026, 10, 17, 5, 0, tzinfo=UTC))    # Saturday
        assert not market_open(T0, holidays=[date(2026, 10, 19)])

    def test_backoff_doubles_and_caps(self):
        a = Fixture()
        assert [retry_delay(a, n) for n in (0, 1, 2, 3)] == [60, 120, 240, 480] and retry_delay(a, 20) == 3600

    def test_due_respects_interval_and_backoff(self, env):
        a = Fixture()
        assert due([a], T0, env["health"]) == [a]                           # never run
        go(a, env)
        assert due([a], T0 + timedelta(seconds=30), env["health"]) == []
        assert due([a], T0 + timedelta(seconds=61), env["health"]) == [a]

    def test_staleness_alerts_only_while_the_market_is_open(self, env):
        class Live(Fixture):
            name, market_hours_only = "live", True

        a = Live([csv_item([("AAA", "2026-10-19T04:00:00Z", 1.0)], T0)])
        go(a, env)
        stale = evaluate_staleness([a], env["health"], T0 + timedelta(hours=1), env["alerts"])
        assert stale == ["live"] and env["health"].get("live").status == "stale"
        n = len(env["alerts"].sent)
        after_close = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
        assert evaluate_staleness([a], env["health"], after_close, env["alerts"]) == [] and len(env["alerts"].sent) == n

    def test_one_failing_source_never_stops_the_others(self, env):
        good = Fixture([csv_item(ROW, T0)])
        bad = Fixture(fail=ConnectionError("down"))
        bad.name = "bad"
        results = run_cycle([bad, good], T0, env["landing"], env["sink"], env["health"], env["cp"], env["alerts"])
        by = {r.source: r for r in results}
        assert not by["bad"].ok and by["fixture"].ok and len(env["sink"].frame("fixture")) == 1


class TestPgHealthStore:
    def test_roundtrip_against_the_database(self):
        try:
            from qsde.db.connection import execute_sql
            from qsde.stream.health import PgHealthStore
            s = PgHealthStore()
            h = s.get("pytest_probe")
        except Exception as e:  # noqa: BLE001 - hermetic CI without the database
            pytest.skip(f"database unavailable: {e}")
        h.status, h.last_success_at, h.consecutive_failures, h.last_error, h.rows_last = "failing", T0, 4, "boom", 7
        s.put(h)
        try:
            g = s.get("pytest_probe")
            assert g.status == "failing" and g.last_success_at == T0 and g.consecutive_failures == 4 and g.rows_last == 7
            assert g.last_error == "boom" and g.last_data_time is None
        finally:
            execute_sql("DELETE FROM source_health WHERE source = 'pytest_probe'")
