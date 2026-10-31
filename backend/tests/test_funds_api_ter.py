"""Expense ratios on the funds API (real database, run locally; CI skips). Rows, sorts, history and wording."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

pytestmark = pytest.mark.real_data
EXPENSE_KEYS = {"status", "reason", "as_of", "plan", "base", "brokerage", "transaction", "statutory", "total", "flags", "flag_text"}


@pytest.fixture()
def client():                                    # function scope: the conftest turns auth off per test
    from api.main import app

    c = TestClient(app)
    r = c.get("/api/funds/schemes?limit=1")
    if r.status_code == 401:
        pytest.skip("API requires auth in this environment")
    if r.status_code != 200 or r.json()["n_matching"] == 0:
        pytest.skip("no funds loaded")
    return c


def test_list_rows_carry_expense_with_a_status_and_the_day_it_is_as_of(client):
    j = client.get("/api/funds/schemes?limit=100").json()
    assert j["expense_as_of"] and len(j["rows"]) == 100
    assert all(r["expense"] and set(r["expense"]) >= EXPENSE_KEYS for r in j["rows"])
    assert sum(1 for r in j["rows"] if r["expense"]["status"] == "ok") >= 40


def test_sort_by_base_expense_ascending_is_ordered_with_missing_last(client):
    rows = client.get("/api/funds/schemes?sort=ter_base&order=asc&limit=100").json()["rows"]
    vals = [r["expense"]["base"] if r["expense"]["status"] == "ok" else None for r in rows]
    seen_none = False
    for a, b in zip(vals, vals[1:]):
        seen_none = seen_none or a is None
        if a is not None and b is not None:
            assert a <= b
        if seen_none:
            assert b is None                              # once the unknowns start they continue to the end


def test_sort_by_total_descending_is_ordered(client):
    rows = client.get("/api/funds/schemes?sort=ter_total&order=desc&limit=100").json()["rows"]
    vals = [r["expense"]["total"] for r in rows if r["expense"]["status"] == "ok"]
    assert len(vals) >= 40 and vals == sorted(vals, reverse=True)


def test_a_page_of_100_rows_does_not_make_a_query_per_row(client, monkeypatch):
    from qsde.db import connection

    n = {"q": 0}
    real = connection.read_sql

    def counting(*a, **k):
        n["q"] += 1
        return real(*a, **k)

    import api.routes.funds as F

    monkeypatch.setattr(F, "read_sql", counting)
    assert client.get("/api/funds/schemes?limit=100&sort=ter_base").status_code == 200
    assert n["q"] <= 6


def test_detail_has_monthly_history_oldest_first_and_the_row_expense(client):
    code = next(r["scheme_code"] for r in client.get("/api/funds/schemes?limit=100").json()["rows"] if r["expense"]["status"] == "ok")
    j = client.get(f"/api/funds/schemes/{code}").json()
    months = [p["month"] for p in j["expense_history"]]
    assert months and months == sorted(set(months)) and j["fund"]["expense"]["status"] == "ok"
    assert set(j["expense_history"][-1]) >= {"month", "as_of", "base", "brokerage", "transaction", "statutory", "total"}


def test_old_fields_are_unchanged(client):
    r = client.get("/api/funds/schemes?limit=1").json()["rows"][0]
    assert {"scheme_code", "name", "amc", "category", "plan", "returns", "risk", "quality", "peers", "snapshot_as_of"} <= set(r)


def test_wording_no_longer_says_expense_ratio_is_missing_but_says_what_it_is(client):
    j = client.get("/api/funds/schemes?limit=1").json()
    assert "expense ratio, holdings" not in j["disclaimer"] and "holdings / overlap and AUM are not loaded" in j["disclaimer"]
    for word in ("base expense ratio", "brokerage", "transaction cost", "statutory", "reset"):
        assert word in j["methodology"].lower()
