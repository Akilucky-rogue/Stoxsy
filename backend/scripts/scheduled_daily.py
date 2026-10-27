"""Entry point for the scheduler (Windows Task Scheduler now, a systemd timer on the Debian host later): run the daily after-close routine, but only on a day that had a regular NSE session.

On a weekend or an exchange holiday there is no new bhavcopy, so the required price step would fail and raise a false incident; this wrapper exits 0 and says why instead.
If the holiday calendar is missing or expired it does NOT skip: running a harmless extra day is better than silently missing one.
Exit code is that of `ingest daily` (0 ok, non-zero failed), so the scheduler and `stoxsy doctor` both see real failures.
"""
from __future__ import annotations

import subprocess
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def should_run(today: date, calendar) -> tuple:
    """(run, reason). Skips only when the calendar covers `today` and says it is not a regular session."""
    if today.weekday() >= 5:
        return False, f"{today} is a weekend"
    if calendar.covers(today) and not calendar.is_session(today):
        return False, f"{today} is an NSE holiday or special-timing day"
    return True, "regular session" if calendar.covers(today) else "calendar does not cover today: running anyway"


def main() -> int:
    from qsde import exchange_calendar

    run, why = should_run(date.today(), exchange_calendar.load())
    print(f"scheduled daily routine: {why}", flush=True)
    if not run:
        return 0
    return subprocess.call([sys.executable, "-m", "qsde.cli", "ingest", "daily"], cwd=str(Path(__file__).resolve().parents[1]))


if __name__ == "__main__":
    raise SystemExit(main())
