"""The scheduler wrapper skips weekends and exchange holidays, never skips on a missing calendar, and runs on a regular session."""
import importlib.util
from datetime import date
from pathlib import Path

from qsde import exchange_calendar as X

spec = importlib.util.spec_from_file_location("scheduled_daily", Path(__file__).resolve().parents[1] / "scripts" / "scheduled_daily.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

CAL = X.from_rows([("CM", date(2026, 11, 10), False), ("CM", date(2026, 12, 25), False)])


def test_regular_session_runs():
    assert mod.should_run(date(2026, 11, 9), CAL)[0] is True


def test_weekend_and_holiday_are_skipped_with_a_reason():
    assert mod.should_run(date(2026, 11, 7), CAL) == (False, "2026-11-07 is a weekend")
    run, why = mod.should_run(date(2026, 11, 10), CAL)
    assert run is False and "holiday" in why


def test_a_missing_or_expired_calendar_never_skips_a_weekday():
    assert mod.should_run(date(2026, 11, 10), X.Calendar())[0] is True
    run, why = mod.should_run(date(2027, 1, 13), CAL)
    assert run is True and "does not cover today" in why
