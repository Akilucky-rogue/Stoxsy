"""Due-time scheduling and staleness evaluation as PURE functions (no threads, no network), plus an opt-in run loop.

Times are Asia/Kolkata for market hours. `market_open` takes an explicit holiday set; the real NSE holiday calendar is a
separate data source (S1) and is injected, never assumed.
"""
from __future__ import annotations

import time
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Callable, Iterable, Optional

from qsde.stream.base import SourceAdapter
from qsde.stream.health import IST, Alert, AlertSink, CheckpointStore, HealthStore
from qsde.stream.landing import LandingZone
from qsde.stream.runner import RunResult, Sink, run_once

MARKET_OPEN, MARKET_CLOSE = dtime(9, 15), dtime(15, 30)
MAX_BACKOFF_S = 3600


def market_open(now: datetime, holidays: Iterable[date] = ()) -> bool:
    t = now.astimezone(IST)
    return t.weekday() < 5 and t.date() not in set(holidays) and MARKET_OPEN <= t.time() <= MARKET_CLOSE


def retry_delay(adapter: SourceAdapter, consecutive_failures: int) -> int:
    """Seconds to wait after a run: the normal interval, doubling per consecutive failure, capped."""
    return int(min(adapter.interval_seconds * (2 ** max(consecutive_failures, 0)), MAX_BACKOFF_S))


def due(adapters: Iterable[SourceAdapter], now: datetime, health: HealthStore, holidays: Iterable[date] = ()) -> list:
    out = []
    for a in adapters:
        if a.market_hours_only and not market_open(now, holidays):
            continue
        h = health.get(a.name)
        if h.last_attempt_at is None or (now - h.last_attempt_at).total_seconds() >= retry_delay(a, h.consecutive_failures):
            out.append(a)
    return out


def evaluate_staleness(adapters: Iterable[SourceAdapter], health: HealthStore, now: datetime, alerts: AlertSink,
                       holidays: Iterable[date] = ()) -> list:
    """Mark sources whose newest data is older than their SLO (only while they should be live) and alert. Returns stale names."""
    stale = []
    for a in adapters:
        if a.market_hours_only and not market_open(now, holidays):
            continue
        h = health.get(a.name)
        ref = h.last_data_time or h.last_success_at
        if ref is None:
            if h.last_attempt_at is not None:
                h.status = "failing" if h.consecutive_failures else h.status
                health.put(h)
            continue
        age = (now - ref).total_seconds()
        if age > a.slo_seconds:
            h.status = "stale"
            health.put(h)
            stale.append(a.name)
            sev = "critical" if age > 3 * a.slo_seconds else "warning"
            alerts.emit(Alert(sev, f"{a.name}: data is stale",
                              f"newest data {age / 60:.0f} min old (SLO {a.slo_seconds / 60:.0f} min)", f"stale:{a.name}", now))
    return stale


def run_cycle(adapters: Iterable[SourceAdapter], now: datetime, landing: LandingZone, sink: Sink, health: HealthStore,
              checkpoints: CheckpointStore, alerts: AlertSink, holidays: Iterable[date] = ()) -> list:
    """Run every due adapter once, then judge staleness. One adapter failing never stops the others."""
    results: list[RunResult] = []
    adapters = list(adapters)
    for a in due(adapters, now, health, holidays):
        results.append(run_once(a, landing, sink, health, checkpoints, alerts, clock=lambda n=now: n))
    evaluate_staleness(adapters, health, now, alerts, holidays)
    return results


def run_forever(adapters, landing, sink, health, checkpoints, alerts, holidays=(), tick_s: int = 5,
                stop: Optional[Callable[[], bool]] = None) -> None:
    """Opt-in loop for `run.ps1 live`. Never started by import; tests drive run_cycle with an explicit clock."""
    while not (stop and stop()):
        run_cycle(adapters, datetime.now(timezone.utc), landing, sink, health, checkpoints, alerts, holidays)
        time.sleep(tick_s)
