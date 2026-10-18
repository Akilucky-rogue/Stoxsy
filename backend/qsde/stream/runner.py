"""One run of one source: fetch -> LAND RAW -> parse -> validate -> stamp known_at -> idempotent commit -> checkpoint -> health.

Guarantees (each covered by tests/test_stream_foundation.py):
  * raw first      the bytes are on disk before the parser sees them; a parse failure never loses data
  * point in time  every row carries event_time and known_at; known_at is never earlier than we actually fetched it
  * idempotent     the same landed bytes always produce the same rows (same known_at), so re-runs and replays add nothing
  * corrections    a re-published/corrected record is fetched later, gets a later known_at, and is a NEW row (never an update)
  * checkpoint     advanced only after the sink has committed the rows
  * visible        success, failure and parse failure all update health; repeated failures raise alerts
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional, Protocol

import pandas as pd

from qsde.stream.base import RawItem, SourceAdapter
from qsde.stream.health import Alert, AlertSink, CheckpointStore, HealthStore, SourceHealth
from qsde.stream.landing import LandingZone

log = logging.getLogger(__name__)

FAILURES_BEFORE_CRITICAL = 3


class Sink(Protocol):
    def upsert(self, source: str, df: pd.DataFrame, key_cols: tuple) -> int: ...


class InMemorySink:
    """Rows keyed by (natural key, known_at): re-processing identical input adds nothing; corrections add rows."""

    def __init__(self):
        self.rows: dict = {}

    def upsert(self, source: str, df: pd.DataFrame, key_cols: tuple) -> int:
        n = 0
        store = self.rows.setdefault(source, {})
        for rec in df.to_dict("records"):
            k = tuple(rec[c] for c in (*key_cols, "known_at"))
            if k not in store:
                store[k] = rec
                n += 1
        return n

    def frame(self, source: str) -> pd.DataFrame:
        return pd.DataFrame(list(self.rows.get(source, {}).values()))


def compute_known_at(event_time, published_at: Optional[datetime], fetched_at: datetime, mode: str = "live",
                     backfill_lag: timedelta = timedelta(hours=1)) -> datetime:
    """The first instant the record could have been known to us.

    live      never earlier than the fetch (we cannot have known it before we got it); if the publisher stamps a LATER
              time, that wins.
    backfill  historical data fetched long after the fact: use max(publisher time or event time) + a conservative lag, never
              the (much later) fetch time, so history is usable, but never earlier than the event itself.
    """
    fetched_at = fetched_at.astimezone(timezone.utc)
    if mode == "live":
        return max(fetched_at, published_at.astimezone(timezone.utc)) if published_at else fetched_at
    if mode == "backfill":
        base = published_at or pd.Timestamp(event_time).to_pydatetime()
        base = base if base.tzinfo else base.replace(tzinfo=timezone.utc)
        return base.astimezone(timezone.utc) + backfill_lag
    raise ValueError("mode must be 'live' or 'backfill'")


def stamp_known_at(df: pd.DataFrame, item: RawItem, mode: str = "live") -> pd.DataFrame:
    out = df.copy()
    out["event_time"] = pd.to_datetime(out["event_time"], utc=True)
    out["known_at"] = [compute_known_at(e, item.published_at, item.fetched_at, mode) for e in out["event_time"]]
    if mode == "live" and (out["known_at"] < out["event_time"]).any():
        # the record claims to describe the future relative to when we got it: knowledge cannot precede the event
        out["known_at"] = out[["known_at", "event_time"]].max(axis=1)
    return out


@dataclass
class RunResult:
    source: str
    ok: bool
    stage: str = "done"                    # fetch | parse | commit | done
    items: int = 0
    rows: int = 0
    errors: list = field(default_factory=list)


def run_once(adapter: SourceAdapter, landing: LandingZone, sink: Sink, health: HealthStore, checkpoints: CheckpointStore,
             alerts: AlertSink, clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
             mode: str = "live") -> RunResult:
    name, now = adapter.name, clock()
    h = health.get(name)
    h.last_attempt_at = now
    res = RunResult(name, ok=True)
    try:
        items = sorted(adapter.fetch(checkpoints.get(name)), key=lambda i: i.fetched_at)
    except Exception as e:  # noqa: BLE001 - any fetch failure is a health event, never a crash of the scheduler
        h.consecutive_failures += 1
        h.status, h.last_error = "failing", f"fetch: {e}"[:300]
        health.put(h)
        sev = "critical" if h.consecutive_failures >= FAILURES_BEFORE_CRITICAL else "warning"
        alerts.emit(Alert(sev, f"{name}: fetch failed ({h.consecutive_failures}x)", h.last_error, f"fetch-fail:{name}", now))
        return RunResult(name, False, "fetch", errors=[h.last_error])

    newest_data = h.last_data_time
    for item in items:
        res.items += 1
        landing.write(name, item)                                   # RAW FIRST
        try:
            df = adapter.parse(item)
            problems = adapter.validate(df)
            if problems:
                raise ValueError("; ".join(problems))
        except Exception as e:  # noqa: BLE001
            res.ok, res.stage = False, "parse"
            res.errors.append(f"parse: {e}"[:300])
            h.status, h.last_error = "parse_failed", res.errors[-1]
            alerts.emit(Alert("critical", f"{name}: parse/validation failed", res.errors[-1], f"parse-fail:{name}", now))
            continue                                                 # raw is safe; replay after the parser is fixed
        df = stamp_known_at(df, item, mode)
        res.rows += sink.upsert(name, df, adapter.key_cols)
        checkpoints.advance(name, item.fetched_at)                   # only after the commit
        if len(df):
            t = df["event_time"].max().to_pydatetime()
            newest_data = t if newest_data is None or t > newest_data else newest_data

    if res.ok:
        h.consecutive_failures, h.last_error, h.status = 0, None, "ok"
        h.last_success_at, h.rows_last = now, res.rows
    else:
        h.consecutive_failures += 1
    h.last_data_time = newest_data
    health.put(h)
    return res


def replay(adapter: SourceAdapter, landing: LandingZone, sink: Sink, mode: str = "live",
           since: Optional[datetime] = None) -> RunResult:
    """Re-parse every landed raw record (no network). Deterministic: same bytes -> same rows -> nothing new for the sink."""
    res = RunResult(adapter.name, ok=True)
    for rec in landing.records(adapter.name, since):
        item = landing.read(rec)
        res.items += 1
        try:
            df = adapter.parse(item)
            problems = adapter.validate(df)
            if problems:
                raise ValueError("; ".join(problems))
        except Exception as e:  # noqa: BLE001
            res.ok, res.stage = False, "parse"
            res.errors.append(f"{rec.path.name}: {e}"[:300])
            continue
        res.rows += sink.upsert(adapter.name, stamp_known_at(df, item, mode), adapter.key_cols)
    return res
