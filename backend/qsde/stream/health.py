"""Source health (is data flowing?), checkpoints (where did we get to?) and alert emission, with in-memory and durable stores.

Health is data: every run records success/failure, the newest data time and the lag, so a silent feed shows up as a stale
source instead of as quietly-wrong features.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Protocol

log = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))


@dataclass
class SourceHealth:
    source: str
    last_attempt_at: Optional[datetime] = None
    last_success_at: Optional[datetime] = None
    last_data_time: Optional[datetime] = None
    consecutive_failures: int = 0
    last_error: Optional[str] = None
    rows_last: int = 0
    status: str = "unknown"                   # unknown | ok | failing | stale | parse_failed


class HealthStore(Protocol):
    def get(self, source: str) -> SourceHealth: ...
    def put(self, h: SourceHealth) -> None: ...
    def all(self) -> list: ...


class InMemoryHealthStore:
    def __init__(self):
        self._d: dict = {}

    def get(self, source: str) -> SourceHealth:
        return self._d.get(source) or SourceHealth(source)

    def put(self, h: SourceHealth) -> None:
        self._d[h.source] = h

    def all(self) -> list:
        return list(self._d.values())


class JsonHealthStore(InMemoryHealthStore):
    """Durable without a database: one JSON file, rewritten atomically."""

    def __init__(self, path: Path):
        super().__init__()
        self.path = Path(path)
        if self.path.exists():
            for k, v in json.loads(self.path.read_text(encoding="utf-8")).items():
                for f in ("last_attempt_at", "last_success_at", "last_data_time"):
                    v[f] = datetime.fromisoformat(v[f]) if v.get(f) else None
                self._d[k] = SourceHealth(**v)

    def put(self, h: SourceHealth) -> None:
        super().put(h)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        out = {k: {**asdict(v), **{f: (getattr(v, f).isoformat() if getattr(v, f) else None)
                                   for f in ("last_attempt_at", "last_success_at", "last_data_time")}}
               for k, v in self._d.items()}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(out, indent=1), encoding="utf-8")
        tmp.replace(self.path)


class CheckpointStore:
    """High-water mark per source: the newest `fetched_at` whose rows are committed. Advanced only after the sink commits."""

    def __init__(self, path: Optional[Path] = None):
        self.path, self._d = (Path(path) if path else None), {}
        if self.path and self.path.exists():
            self._d = json.loads(self.path.read_text(encoding="utf-8"))

    def get(self, source: str) -> Optional[datetime]:
        v = self._d.get(source)
        return datetime.fromisoformat(v) if v else None

    def advance(self, source: str, t: datetime) -> None:
        cur = self.get(source)
        if cur is None or t > cur:
            self._d[source] = t.isoformat()
            if self.path:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text(json.dumps(self._d), encoding="utf-8")
                tmp.replace(self.path)


@dataclass
class Alert:
    severity: str               # info | warning | critical
    title: str
    body: str
    dedup_key: str
    ts: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class AlertSink(Protocol):
    def emit(self, alert: Alert) -> bool: ...


class LogAlertSink:
    """Minimal producer-side alert sink: logs, optionally appends to JSONL, de-duplicates inside a cooldown.

    The user-facing alert system (rules, channels, escalation) consumes this stream later; producers only need emit()."""

    def __init__(self, path: Optional[Path] = None, cooldown_s: int = 900):
        self.path, self.cooldown = (Path(path) if path else None), timedelta(seconds=cooldown_s)
        self._last: dict = {}
        self.sent: list = []

    def emit(self, alert: Alert) -> bool:
        last = self._last.get(alert.dedup_key)
        if last is not None and alert.ts - last < self.cooldown:
            return False
        self._last[alert.dedup_key] = alert.ts
        self.sent.append(alert)
        log.log(logging.ERROR if alert.severity == "critical" else logging.WARNING, "ALERT [%s] %s: %s",
                alert.severity, alert.title, alert.body)
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps({**asdict(alert), "ts": alert.ts.isoformat()}) + "\n")
        return True


class PgHealthStore:
    """TimescaleDB-backed health (table `source_health`, migration 031). Same interface as the in-memory store."""

    COLS = ("status", "last_attempt_at", "last_success_at", "last_data_time", "consecutive_failures", "last_error", "rows_last")

    def get(self, source: str) -> SourceHealth:
        from qsde.db.connection import read_sql
        d = read_sql("SELECT * FROM source_health WHERE source = :s", params={"s": source})
        if d.empty:
            return SourceHealth(source)
        r = d.iloc[0]
        t = lambda v: None if v is None or v != v else v.to_pydatetime()      # noqa: E731
        return SourceHealth(source, t(r["last_attempt_at"]), t(r["last_success_at"]), t(r["last_data_time"]),
                            int(r["consecutive_failures"]), r["last_error"] if r["last_error"] == r["last_error"] else None,
                            int(r["rows_last"]), r["status"])

    def put(self, h: SourceHealth) -> None:
        from qsde.db.connection import execute_sql
        execute_sql("""INSERT INTO source_health (source, status, last_attempt_at, last_success_at, last_data_time,
                          consecutive_failures, last_error, rows_last, updated_at)
                       VALUES (%(s)s,%(st)s,%(a)s,%(ok)s,%(dt)s,%(f)s,%(e)s,%(n)s,NOW())
                       ON CONFLICT (source) DO UPDATE SET status=EXCLUDED.status, last_attempt_at=EXCLUDED.last_attempt_at,
                          last_success_at=EXCLUDED.last_success_at, last_data_time=EXCLUDED.last_data_time,
                          consecutive_failures=EXCLUDED.consecutive_failures, last_error=EXCLUDED.last_error,
                          rows_last=EXCLUDED.rows_last, updated_at=NOW()""",
                    {"s": h.source, "st": h.status, "a": h.last_attempt_at, "ok": h.last_success_at, "dt": h.last_data_time,
                     "f": h.consecutive_failures, "e": h.last_error, "n": h.rows_last})

    def all(self) -> list:
        from qsde.db.connection import read_sql
        return [self.get(s) for s in read_sql("SELECT source FROM source_health")["source"]]
