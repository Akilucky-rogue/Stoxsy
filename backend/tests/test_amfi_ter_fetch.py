"""`stoxsy fetch-ter`: fetch AMFI's own monthly TER workbook into the inbox. Logic tests use plain dates and month names (no market data); the payload and save tests use the owner's REAL workbooks;
the one live test (a single month-list request) is `real_data`, so CI skips it. Nothing here fabricates data."""
from __future__ import annotations

import hashlib
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from amfi_ter_support import real_xlsx_files, require_real_file  # noqa: E402

from qsde.ingestion import amfi_ter_fetch as F  # noqa: E402


def test_financial_year_runs_april_to_march():
    assert F.financial_year("10-2026") == "2026-2027"
    assert F.financial_year("04-2026") == "2026-2027"
    assert F.financial_year("03-2027") == "2026-2027"
    assert F.financial_year("01-2026") == "2025-2026"


def test_parse_months_keeps_only_well_formed_months_newest_first():
    payload = [{"MonthYear": "October-2026", "MonthNumber": "10-2026"}, {"MonthYear": "x", "MonthNumber": "13-2026"}, {"MonthNumber": "9-2026"}, {"MonthNumber": "09-2026"}, "junk", {"MonthNumber": "10-2026"}]
    assert F.parse_months(payload) == ["10-2026", "09-2026"]
    with pytest.raises(ValueError):
        F.parse_months({"error": "x"})


def test_plan_fetches_a_month_never_loaded_and_a_month_that_stopped_short():
    today = date(2026, 10, 30)
    avail = ["10-2026", "09-2026", "08-2026"]
    loaded = {"10-2026": date(2026, 10, 29), "09-2026": date(2026, 9, 30)}
    seen = {"10-2026": date(2026, 10, 30), "09-2026": date(2026, 10, 10)}
    assert F.plan(avail, loaded, seen, today, recent=3, limit=5) == ["08-2026"]                         # October is current to yesterday; September is whole and settled
    assert F.plan(avail, {**loaded, "10-2026": date(2026, 10, 20)}, seen, today) == ["10-2026"]          # October is 10 days behind
    assert F.plan(avail, {**loaded, "09-2026": date(2026, 9, 28)}, seen, today) == ["09-2026"]           # a finished month that stopped short is fetched again


def test_plan_fetches_a_finished_month_once_more_after_late_filings_settle():
    """AMFI fills in late: a month's last days and some schemes' early days arrive after the month ends, so a month last fetched before it settled is fetched again."""
    today = date(2026, 10, 30)
    loaded = {"10-2026": date(2026, 10, 29), "09-2026": date(2026, 9, 30)}
    assert F.plan(["10-2026", "09-2026"], loaded, {"10-2026": date(2026, 10, 30), "09-2026": date(2026, 9, 30)}, today) == ["09-2026"]   # last fetched on its last day
    assert F.plan(["10-2026", "09-2026"], loaded, {"10-2026": date(2026, 10, 30), "09-2026": date(2026, 10, 2)}, today) == ["09-2026"]    # two days after the end: not yet settled
    assert F.plan(["10-2026", "09-2026"], loaded, {"10-2026": date(2026, 10, 30), "09-2026": date(2026, 10, 3)}, today) == []            # three days after: settled
    assert F.plan(["10-2026", "09-2026"], loaded, {"10-2026": date(2026, 10, 30)}, today) == ["09-2026"]                                   # never recorded as fetched


def test_plan_looks_only_at_recent_months_and_never_exceeds_the_limit():
    today = date(2026, 10, 30)
    avail = ["10-2026", "09-2026", "08-2026", "07-2026", "06-2026"]
    assert F.plan(avail, {}, {}, today) == ["10-2026", "09-2026"]                                      # default: this and last month only
    assert F.plan(avail, {}, {}, today, recent=5, limit=3) == ["10-2026", "09-2026", "08-2026"]        # newest first, capped
    assert F.plan(avail, {}, {}, today, recent=5, limit=0) == []


def test_plan_ignores_months_not_yet_started():
    assert F.plan(["11-2026", "10-2026"], {}, {}, date(2026, 10, 30), recent=2) == ["10-2026"]


def test_check_payload_accepts_a_real_workbook_and_refuses_anything_else():
    real = require_real_file().read_bytes()
    F.check_payload(real, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")        # no exception
    for bad_body, bad_type in [(b"<html>blocked</html>", "text/html"), (b"", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
                               (b"<html>blocked</html>", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"), (real, "text/html"), (real[:5000], "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")]:
        with pytest.raises(F.BadDownload):
            F.check_payload(bad_body, bad_type)


def test_save_names_the_file_by_month_and_day_and_skips_content_already_in_the_inbox(tmp_path):
    real = real_xlsx_files()
    if not real:
        pytest.skip("no real AMFI TER file in the inbox")
    body = real[0].read_bytes()
    first = F.save(body, "10-2026", date(2026, 10, 30), tmp_path)
    assert first is not None and first.name == "ter_revised_10-2026__2026-10-30.xlsx" and first.read_bytes() == body
    assert not list(tmp_path.glob("*.part"))
    assert F.save(body, "10-2026", date(2026, 10, 31), tmp_path) is None                              # same bytes: nothing new to keep
    assert F.save(body, "09-2026", date(2026, 10, 31), tmp_path) is None
    assert sorted(p.name for p in tmp_path.iterdir()) == ["ter_revised_10-2026__2026-10-30.xlsx"]
    assert hashlib.sha256(first.read_bytes()).hexdigest() == hashlib.sha256(body).hexdigest()


@pytest.mark.real_data
def test_live_month_list_names_the_latest_loaded_month():
    """One real request to AMFI's month list (metadata only, no scheme data): the current financial year's list has the month the database already holds."""
    months = F.list_months(F.financial_year(date.today().strftime("%m-%Y")))
    assert months and months == sorted(months, key=lambda m: (m[3:], m[:2]), reverse=True)
    loaded = {m for m in ("10-2026", "09-2026")}
    assert loaded & set(months)
