"""NSE session calendar: which dates are sessions, and the date n sessions ahead, with an honest 'estimated' flag.

A date is a regular session when it is a weekday, not in the loaded holiday list for the segment, and not a special-timing day (Muhurat). The published list covers one calendar year at a time,
so the calendar knows its own horizon (`loaded_through` = 31 December of the latest year loaded). Past that horizon the answer falls back to weekdays AND says `estimated = True`;
nothing falls back silently. `status` reports the days left so `stoxsy doctor` and the alerts can warn before the horizon is reached.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

WARN_DAYS = 45


@dataclass(frozen=True)
class Calendar:
    holidays: frozenset = field(default_factory=frozenset)              # dates that are not regular sessions on a weekday (full holidays and special-timing days)
    special: frozenset = field(default_factory=frozenset)               # the subset NSE marks as special-timing (Muhurat)
    loaded_through: Optional[date] = None                               # None = nothing loaded

    def is_session(self, d: date) -> bool:
        return d.weekday() < 5 and d not in self.holidays

    def covers(self, d: date) -> bool:
        return self.loaded_through is not None and d <= self.loaded_through

    def add_sessions(self, start: date, n: int) -> tuple:
        """(date, estimated): the date n regular sessions after `start`. estimated is True if any day on the way lies beyond the loaded horizon."""
        d, k, est = start, 0, False
        while k < n:
            d += timedelta(days=1)
            if not self.covers(d):
                est = True
            if self.is_session(d):
                k += 1
        return d, est

    def sessions_between(self, a: date, b: date) -> int:
        """Regular sessions in (a, b]."""
        n, d = 0, a
        while d < b:
            d += timedelta(days=1)
            n += 1 if self.is_session(d) else 0
        return n

    def status(self, today: date) -> dict:
        if self.loaded_through is None:
            return {"state": "missing", "loaded_through": None, "days_left": None, "message": "no exchange holiday calendar is loaded: session dates are weekday estimates"}
        left = (self.loaded_through - today).days
        if left < 0:
            return {"state": "expired", "loaded_through": str(self.loaded_through), "days_left": left, "message": f"the holiday calendar ended on {self.loaded_through}: session dates after it are weekday estimates"}
        if left < WARN_DAYS:
            return {"state": "ending", "loaded_through": str(self.loaded_through), "days_left": left, "message": f"the holiday calendar ends on {self.loaded_through} ({left} days): load next year's list when NSE publishes it"}
        return {"state": "ok", "loaded_through": str(self.loaded_through), "days_left": left, "message": f"holiday calendar loaded through {self.loaded_through}"}


def from_rows(rows, segment: str = "CM") -> Calendar:
    """rows: iterable of (segment, holiday_date, special_session)."""
    hol, spec, latest = set(), set(), None
    for seg, d, sp in rows:
        if seg != segment:
            continue
        hol.add(d)
        if sp:
            spec.add(d)
        latest = d if latest is None or d > latest else latest
    return Calendar(frozenset(hol), frozenset(spec), None if latest is None else date(latest.year, 12, 31))


def load(read_sql=None, segment: str = "CM") -> Calendar:
    """Read the stored calendar. Never raises: a database problem yields an empty calendar, which reports itself as 'missing'."""
    try:
        if read_sql is None:
            from qsde.db.connection import read_sql
        d = read_sql("SELECT segment, holiday_date, special_session FROM exchange_holidays WHERE exchange = 'NSE'")
        return from_rows([(r.segment, r.holiday_date, bool(r.special_session)) for r in d.itertuples()], segment)
    except Exception:                                                            # noqa: BLE001
        return Calendar()
