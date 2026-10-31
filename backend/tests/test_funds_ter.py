"""Expense ratio per fund (qsde/funds/ter.py), read from the REAL database loaded from AMFI's files. Run locally (`real_data`); CI skips them. Nothing here fabricates AMFI data:
the one test that needs a revision (none has occurred in the real files yet) writes it inside a rolled-back transaction."""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qsde.db.connection import get_sync_conn  # noqa: E402
from qsde.funds import ter as T  # noqa: E402
from qsde.ingestion.amfi_ter_db import ACTIVE_DAYS, CARRY_DAYS, coverage, latest_complete_date  # noqa: E402

pytestmark = pytest.mark.real_data
STATUSES = {"ok", "not_offered", "stale", "not_covered"}
COMPONENTS = ("base", "brokerage", "transaction", "statutory", "total")
COLS = {"base": "ber", "brokerage": "brokerage", "transaction": "transaction", "statutory": "statutory", "total": "total"}


@pytest.fixture(scope="module")
def conn():
    with get_sync_conn() as c:
        cur = c.cursor()
        cur.execute("SELECT count(*) AS n FROM mf_ter")
        if cur.fetchone()["n"] == 0:
            pytest.skip("no TER data loaded")
        yield c


@pytest.fixture(scope="module")
def as_of(conn):
    return latest_complete_date(conn.cursor())


def _mapped(conn, n):
    cur = conn.cursor()
    cur.execute("SELECT scheme_code FROM mf_ter_map ORDER BY scheme_code")
    codes = [r["scheme_code"] for r in cur.fetchall()]
    return codes[:: max(len(codes) // n, 1)][:n]


def test_values_equal_the_stored_row_exactly(conn, as_of):
    codes = _mapped(conn, 200)
    got = T.expense_for(codes, as_of, conn=conn)
    cur = conn.cursor()
    checked = 0
    for code in codes:
        e = got[code]
        if e["status"] not in ("ok", "not_offered"):
            continue
        cur.execute("""SELECT m.plan, t.* FROM mf_ter_map m JOIN LATERAL (SELECT * FROM mf_ter WHERE nsdl_code = m.nsdl_code AND ter_date <= %s ORDER BY ter_date DESC LIMIT 1) t ON TRUE
                       WHERE m.scheme_code = %s AND m.plan = %s""", (as_of, code, e["plan"]))
        row = cur.fetchone()
        assert str(row["ter_date"]) == e["as_of"] and list(row["flags"]) == e["flags"]
        for k, col in COLS.items():
            want = row[f"{e['plan'][0]}_{col}"]
            assert (e[k] is None and want is None) or e[k] == float(want), (code, k)
        checked += 1
    assert checked >= 100


def test_every_requested_code_gets_an_entry_with_a_known_status(conn, as_of):
    codes = _mapped(conn, 50) + [-1, -2]
    got = T.expense_for(codes, as_of, conn=conn)
    assert set(got) == set(codes) and all(v["status"] in STATUSES for v in got.values())
    assert all(set(v) >= {"status", "reason", "as_of", "plan", *COMPONENTS, "flags", "flag_text"} for v in got.values())


def test_an_unmapped_code_is_not_covered_with_a_reason_and_no_numbers(conn, as_of):
    e = T.expense_for([-1], as_of, conn=conn)[-1]
    assert e["status"] == "not_covered" and e["reason"] and all(e[k] is None for k in COMPONENTS) and e["flags"] == []


def test_a_figure_older_than_the_carry_window_is_stale_and_says_which_day(conn, as_of):
    codes = _mapped(conn, 40)
    got = T.expense_for(codes, date(2026, 12, 31), conn=conn)                   # far later than any loaded day: every figure is old
    stale = [v for v in got.values() if v["status"] == "stale"]
    assert len(stale) >= 30
    assert all(v["as_of"] and (date(2026, 12, 31) - date.fromisoformat(v["as_of"])).days > CARRY_DAYS and "older" in v["reason"] for v in stale)


def test_flags_come_with_plain_text(conn, as_of):
    cur = conn.cursor()
    cur.execute("""SELECT m.scheme_code FROM mf_ter_map m JOIN LATERAL (SELECT flags FROM mf_ter WHERE nsdl_code = m.nsdl_code AND ter_date <= %s ORDER BY ter_date DESC LIMIT 1) t ON TRUE
                   WHERE cardinality(t.flags) > 0 LIMIT 30""", (as_of,))
    codes = [r["scheme_code"] for r in cur.fetchall()]
    assert codes, "no flagged scheme in the real data to check"
    got = T.expense_for(codes, as_of, conn=conn)
    for v in got.values():
        assert v["flags"] and len(v["flag_text"]) == len(v["flags"]) and all(isinstance(t, str) and len(t) > 20 for t in v["flag_text"])
        assert set(v["flags"]) <= set(T.FLAG_TEXT)


def test_coverage_equals_the_doctor_checks_count(conn, as_of):
    cur = conn.cursor()
    today = date.today()
    cur.execute("SELECT scheme_code FROM mf_scheme_master WHERE last_nav_date >= %s::date - %s", (today, ACTIVE_DAYS))
    scope = [r["scheme_code"] for r in cur.fetchall()]
    got = T.expense_for(scope, as_of, conn=conn)
    ok = sum(1 for v in got.values() if v["status"] == "ok")
    assert ok == coverage(conn, today)["mapped"]


def test_history_has_one_point_per_month_oldest_first_and_shows_each_months_own_values(conn, as_of):
    cur = conn.cursor()
    for code in _mapped(conn, 20):
        h = T.expense_history(code, 12, conn=conn)
        assert h, code
        months = [p["month"] for p in h]
        assert months == sorted(set(months)) and len(months) <= 12
        e = T.expense_for([code], as_of, conn=conn)[code]
        cur.execute("SELECT m.nsdl_code, m.plan FROM mf_ter_map m WHERE scheme_code = %s", (code,))
        m = cur.fetchone()
        for p in h:
            cur.execute(f"SELECT max(ter_date) AS d FROM mf_ter WHERE nsdl_code = %s AND to_char(ter_date, 'YYYY-MM') = %s AND ter_date <= %s", (m["nsdl_code"], p["month"], as_of))
            d = cur.fetchone()["d"]
            cur.execute(f"SELECT {m['plan'][0]}_brokerage AS b, {m['plan'][0]}_ber AS base FROM mf_ter WHERE nsdl_code = %s AND ter_date = %s", (m["nsdl_code"], d))
            row = cur.fetchone()
            assert p["as_of"] == str(d) and p["brokerage"] == (None if row["b"] is None else float(row["b"])) and p["base"] == (None if row["base"] is None else float(row["base"]))
    assert e is not None


def test_many_codes_cost_a_constant_number_of_queries(conn, as_of):
    class Counting:
        def __init__(self, inner):
            self.inner, self.n = inner, 0

        def execute(self, *a, **k):
            self.n += 1
            return self.inner.execute(*a, **k)

        def __getattr__(self, name):
            return getattr(self.inner, name)

    class Conn:
        def __init__(self, inner):
            self.inner, self.cursors = inner, []

        def cursor(self, *a, **k):
            c = Counting(self.inner.cursor(*a, **k))
            self.cursors.append(c)
            return c

    wrapped = Conn(conn)
    T.expense_for(_mapped(conn, 100), as_of, conn=wrapped)
    assert sum(c.n for c in wrapped.cursors) <= 3


def test_a_revision_overrides_the_first_seen_value_when_read(conn, as_of):
    """No real revision exists yet: one is written inside a rolled-back transaction (a savepoint on the shared connection) to prove the reader uses the current value."""
    cur = conn.cursor()
    code = _mapped(conn, 5)[0]
    before = T.expense_for([code], as_of, conn=conn)[code]
    assert before["status"] == "ok"
    cur.execute("SELECT nsdl_code FROM mf_ter_map WHERE scheme_code = %s", (code,))
    nsdl = cur.fetchone()["nsdl_code"]
    cur.execute("SELECT id FROM mf_ter_files ORDER BY id LIMIT 1")
    fid = cur.fetchone()["id"]
    cur.execute("SAVEPOINT rev")
    try:
        plan_col = before["plan"][0] + "_ber"
        import json
        cur.execute("INSERT INTO mf_ter_revisions (nsdl_code, ter_date, file_id, known_at, old_values, new_values, changed) VALUES (%s, %s, %s, now(), %s, %s, %s)",
                    (nsdl, before["as_of"], fid, json.dumps({plan_col: before["base"]}), json.dumps({plan_col: 9.1234, before["plan"][0] + "_total": 9.5}), [plan_col]))
        after = T.expense_for([code], as_of, conn=conn)[code]
        assert after["base"] == 9.1234 and after["total"] == 9.5 and after["status"] == "ok"
    finally:
        cur.execute("ROLLBACK TO SAVEPOINT rev")
    assert T.expense_for([code], as_of, conn=conn)[code]["base"] == before["base"]


def _with_revision(conn, code, as_of, new_values, flags):
    """Run `fn` with one revision for the scheme's newest row inside a savepoint; returns the expense entry read while it exists."""
    import json

    cur = conn.cursor()
    before = T.expense_for([code], as_of, conn=conn)[code]
    cur.execute("SELECT nsdl_code FROM mf_ter_map WHERE scheme_code = %s", (code,))
    nsdl = cur.fetchone()["nsdl_code"]
    cur.execute("SELECT id FROM mf_ter_files ORDER BY id LIMIT 1")
    fid = cur.fetchone()["id"]
    cur.execute("SAVEPOINT rev2")
    try:
        cur.execute("INSERT INTO mf_ter_revisions (nsdl_code, ter_date, file_id, known_at, old_values, new_values, changed, flags) VALUES (%s, %s, %s, now(), %s, %s, %s, %s)",
                    (nsdl, before["as_of"], fid, json.dumps({}), json.dumps(new_values), ["r_total"], flags))
        return before, T.expense_for([code], as_of, conn=conn)[code]
    finally:
        cur.execute("ROLLBACK TO SAVEPOINT rev2")


def test_a_revision_brings_its_own_flags_not_the_first_seen_rows_flags(conn, as_of):
    cur = conn.cursor()
    cur.execute("""SELECT m.scheme_code, m.plan FROM mf_ter_map m JOIN LATERAL (SELECT flags, r_total, d_total FROM mf_ter WHERE nsdl_code = m.nsdl_code AND ter_date <= %s ORDER BY ter_date DESC LIMIT 1) t ON TRUE
                   WHERE cardinality(t.flags) > 0 AND t.r_total IS NOT NULL AND m.plan = 'regular' LIMIT 1""", (as_of,))
    flagged = cur.fetchone()["scheme_code"]
    before, after = _with_revision(conn, flagged, as_of, {"r_ber": 1.4, "r_total": 1.4}, [])          # AMFI restates a flagged figure to a clean one
    assert before["flags"] and after["flags"] == [] and after["flag_text"] == [] and after["total"] == 1.4
    cur.execute("""SELECT m.scheme_code FROM mf_ter_map m JOIN LATERAL (SELECT flags FROM mf_ter WHERE nsdl_code = m.nsdl_code AND ter_date <= %s ORDER BY ter_date DESC LIMIT 1) t ON TRUE
                   WHERE cardinality(t.flags) = 0 AND m.plan = 'regular' LIMIT 1""", (as_of,))
    clean = cur.fetchone()["scheme_code"]
    before, after = _with_revision(conn, clean, as_of, {"r_ber": 3.4, "r_total": 3.4}, ["total_gt_3_regular"])   # a clean figure restated to a flagged one
    assert before["flags"] == [] and after["flags"] == ["total_gt_3_regular"] and after["flag_text"]


def test_a_figure_exactly_at_the_carry_limit_is_still_ok(conn, as_of):
    cur = conn.cursor()
    cur.execute("SELECT m.scheme_code, max(t.ter_date) AS d FROM mf_ter_map m JOIN mf_ter t ON t.nsdl_code = m.nsdl_code AND t.ter_date <= %s GROUP BY m.scheme_code LIMIT 1", (as_of,))
    r = cur.fetchone()
    from datetime import timedelta

    day = r["d"] + timedelta(days=CARRY_DAYS)
    e = T.expense_for([r["scheme_code"]], day, conn=conn)[r["scheme_code"]]
    assert e["status"] in ("ok", "not_offered") and e["as_of"] == str(r["d"])                         # 7 days old is within the 7-day carry (the doctor counts it covered)
