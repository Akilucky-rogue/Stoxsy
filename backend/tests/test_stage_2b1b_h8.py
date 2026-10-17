"""Regression tests for Stage 2B.1b (AUDIT_v2 H8 — paper next-open fill).

H8 bug: paper trades were entered at signal-day close, which is unattainable
because the EOD job runs after 15:45 IST. The system optimistically claimed
you filled at that close and reconciled against forward bars.

Fix:
  * take_trade / take_baseline_trades / take_tier1_trades insert with
    entry_price = NULL, entry_fill_date = NULL (pending).
  * New fill_pending_entries() stamps the NEXT session's OPEN price and
    entry_fill_date on T+1 EOD.
  * reconcile_open_trades skips pending trades (entry_price IS NULL).
  * daily_eod.py runs fill_pending_entries before reconcile_open_trades.

Hermetic — no DB, no network. Monkey-patches read_sql / execute_sql.
"""
from __future__ import annotations

import inspect
from datetime import date, timedelta
from unittest.mock import MagicMock

import pandas as pd
import pytest


# ─────────────────────────────────────────────────────────────────────
# _next_session_open — pure helper
# ─────────────────────────────────────────────────────────────────────

class TestNextSessionOpen:
    """Given a symbol and an entry_date, return the next session's OPEN
    price and date. This is the actionable transaction price."""

    def test_returns_first_open_strictly_after_entry(self, monkeypatch):
        from qsde.execution import paper_journal
        # Fake read_sql: emulate an OHLCV query "date > X ORDER BY date LIMIT 1"
        # by returning the first (open, date) row.
        def fake_read_sql(sql, params=None):
            # Confirm the query is asking for open + date filtered strictly
            # after `d`, ordered ASC.
            assert "date > :d" in sql, "must use strict > (not >=)"
            assert "ORDER BY date ASC" in sql
            return pd.DataFrame({
                "open": [102.5],
                "date": [pd.Timestamp("2026-06-16")],
            })
        monkeypatch.setattr(paper_journal, "read_sql", fake_read_sql)

        result = paper_journal._next_session_open("RELIANCE", date(2026, 6, 15))
        assert result is not None
        open_px, fill_dt = result
        assert open_px == pytest.approx(102.5)
        assert fill_dt == date(2026, 6, 16)

    def test_returns_none_when_no_next_bar_yet(self, monkeypatch):
        """The T+1 OHLCV hasn't arrived yet (holiday, ingest lag).
        Must return None — the trade stays pending."""
        from qsde.execution import paper_journal
        monkeypatch.setattr(paper_journal, "read_sql",
                            lambda sql, params=None: pd.DataFrame())
        result = paper_journal._next_session_open("RELIANCE", date(2026, 6, 15))
        assert result is None


# ─────────────────────────────────────────────────────────────────────
# fill_pending_entries — core H8 fix
# ─────────────────────────────────────────────────────────────────────

class TestFillPendingEntries:
    def test_no_pending_returns_zero(self, monkeypatch):
        from qsde.execution import paper_journal
        monkeypatch.setattr(paper_journal, "read_sql",
                            lambda sql, params=None: pd.DataFrame())
        r = paper_journal.fill_pending_entries()
        assert r == {"checked": 0, "filled": 0, "still_pending": 0, "errors": []}

    def test_fills_pending_from_next_open(self, monkeypatch):
        from qsde.execution import paper_journal
        # Two pending trades. Both have a next-day bar available.
        pending = pd.DataFrame({
            "id":         [1, 2],
            "symbol":     ["RELIANCE", "TCS"],
            "entry_date": [pd.Timestamp("2026-06-15"),
                           pd.Timestamp("2026-06-15")],
        })
        next_opens = {
            "RELIANCE": (2755.0, date(2026, 6, 16)),
            "TCS":      (4210.5, date(2026, 6, 16)),
        }
        # Track UPDATE calls so we can assert what got stamped.
        updates: list[dict] = []

        def fake_read_sql(sql, params=None):
            if "FROM paper_trades" in sql:
                return pending.copy()
            # _next_session_open query
            sym = params["s"]
            open_px, dt = next_opens[sym]
            return pd.DataFrame({"open": [open_px], "date": [pd.Timestamp(dt)]})

        def fake_execute_sql(sql, params):
            updates.append(dict(params))

        monkeypatch.setattr(paper_journal, "read_sql", fake_read_sql)
        monkeypatch.setattr(paper_journal, "execute_sql", fake_execute_sql)

        r = paper_journal.fill_pending_entries()

        assert r["checked"] == 2
        assert r["filled"] == 2
        assert r["still_pending"] == 0
        assert r["errors"] == []
        # Both UPDATEs must set entry_price = next-open and fill_date = T+1.
        by_id = {u["id"]: u for u in updates}
        assert by_id[1]["px"] == pytest.approx(2755.0)
        assert by_id[1]["fd"] == date(2026, 6, 16)
        assert by_id[2]["px"] == pytest.approx(4210.5)

    def test_leaves_pending_when_next_bar_missing(self, monkeypatch):
        """Holiday / ingest lag: no next-day bar yet. Trade stays pending;
        do NOT stamp entry_price with garbage."""
        from qsde.execution import paper_journal
        pending = pd.DataFrame({
            "id":         [1],
            "symbol":     ["RELIANCE"],
            "entry_date": [pd.Timestamp("2026-06-15")],
        })
        updates: list[dict] = []

        def fake_read_sql(sql, params=None):
            if "FROM paper_trades" in sql:
                return pending.copy()
            # Simulate no next bar available
            return pd.DataFrame()

        def fake_execute_sql(sql, params):
            updates.append(dict(params))

        monkeypatch.setattr(paper_journal, "read_sql", fake_read_sql)
        monkeypatch.setattr(paper_journal, "execute_sql", fake_execute_sql)

        r = paper_journal.fill_pending_entries()
        assert r["filled"] == 0
        assert r["still_pending"] == 1
        assert len(updates) == 0, "must not UPDATE when next bar is missing"


# ─────────────────────────────────────────────────────────────────────
# reconcile_open_trades must skip pending
# ─────────────────────────────────────────────────────────────────────

class TestReconcileSkipsPending:
    def test_reconcile_sql_filters_by_entry_price_not_null(self):
        """Source-level guard: the SELECT that pulls OPEN trades must
        exclude rows still waiting for their next-open fill."""
        from qsde.execution import paper_journal
        src = inspect.getsource(paper_journal.reconcile_open_trades)
        # The fixed query filters entry_price IS NOT NULL AND
        # entry_fill_date IS NOT NULL. Verify both.
        assert "entry_price IS NOT NULL" in src, (
            "reconcile_open_trades must skip rows with entry_price NULL "
            "(pending fill) — otherwise it'll try to compute a return "
            "against a None entry and crash."
        )
        assert "entry_fill_date IS NOT NULL" in src, (
            "reconcile_open_trades must also require entry_fill_date NOT NULL "
            "as a belt-and-braces guard."
        )


# ─────────────────────────────────────────────────────────────────────
# take_trade must insert as PENDING
# ─────────────────────────────────────────────────────────────────────

class TestTakeTradeInsertsPending:
    def test_take_trade_inserts_entry_price_null_by_default(self, monkeypatch):
        from qsde.execution import paper_journal
        # Simulate a signal row for RELIANCE / swing.
        sig = pd.DataFrame({
            "symbol":        ["RELIANCE"],
            "horizon":       ["swing"],
            "direction":     [1],
            "entry_price":   [2500.0],
            "target_price":  [2600.0],
            "stop_price":    [2450.0],
            "ranking_score": [0.92],
            "is_liquid":     [True],
            "date":          [pd.Timestamp("2026-06-15")],
        })
        captured: dict = {}

        def fake_read_sql(sql, params=None):
            return sig.copy()

        def fake_execute_sql(sql, params):
            captured.update(params)

        monkeypatch.setattr(paper_journal, "read_sql", fake_read_sql)
        monkeypatch.setattr(paper_journal, "execute_sql", fake_execute_sql)

        r = paper_journal.take_trade("RELIANCE", "swing", strategy="model")
        assert r["ok"] is True
        assert r.get("pending_fill") is True, (
            "take_trade with no explicit entry_price must mark the trade "
            "as pending_fill (H8)."
        )
        # The INSERT must have entry_price=NULL and entry_fill_date=NULL.
        assert captured["e"] is None, (
            f"entry_price param must be None (SQL NULL); got {captured['e']}"
        )
        assert captured["fd"] is None, (
            "entry_fill_date must be NULL until fill_pending_entries runs"
        )

    def test_take_trade_with_explicit_entry_is_treated_as_filled(self, monkeypatch):
        """If a caller passes an explicit entry_price, respect it — that's
        the escape hatch for tools/tests that already know the fill price."""
        from qsde.execution import paper_journal
        sig = pd.DataFrame({
            "symbol":        ["RELIANCE"],
            "horizon":       ["swing"],
            "direction":     [1],
            "entry_price":   [2500.0],
            "target_price":  [2600.0],
            "stop_price":    [2450.0],
            "ranking_score": [0.92],
            "is_liquid":     [True],
            "date":          [pd.Timestamp("2026-06-15")],
        })
        captured: dict = {}
        monkeypatch.setattr(paper_journal, "read_sql",
                            lambda sql, params=None: sig.copy())
        monkeypatch.setattr(paper_journal, "execute_sql",
                            lambda sql, params: captured.update(params))
        r = paper_journal.take_trade("RELIANCE", "swing", strategy="model",
                                     entry_price=2510.0)
        assert r["ok"] is True
        assert r.get("pending_fill") is False
        assert captured["e"] == pytest.approx(2510.0)
        # fill_date must be TODAY (treating as immediate fill).
        assert captured["fd"] == date.today()


# ─────────────────────────────────────────────────────────────────────
# Migration 019 exists and does the right thing
# ─────────────────────────────────────────────────────────────────────

class TestMigration019:
    def test_migration_019_makes_entry_price_nullable_and_adds_fill_date(self):
        from pathlib import Path
        repo_root = Path(__file__).resolve().parents[2]
        mig = repo_root / "infra" / "migrations" / "019_paper_next_open_fill.sql"
        assert mig.exists(), f"Migration 019 must exist at {mig}"
        sql = mig.read_text()
        # Executable-only
        exec_sql = "\n".join(
            line for line in sql.splitlines()
            if not line.lstrip().startswith("--")
        )
        # Must drop NOT NULL on entry_price
        assert "ALTER COLUMN entry_price DROP NOT NULL" in exec_sql, (
            "Migration 019 must make entry_price nullable — the H8 fix "
            "inserts NULL for pending rows."
        )
        # Must add entry_fill_date column
        assert "ADD COLUMN IF NOT EXISTS entry_fill_date DATE" in exec_sql, (
            "Migration 019 must add entry_fill_date column"
        )
        # Must backfill grandfathered rows so reconcile still works on them
        assert "SET entry_fill_date = entry_date" in exec_sql, (
            "Migration 019 must backfill existing rows with fill_date = entry_date "
            "so reconciliation continues to work on grandfathered trades."
        )


# ─────────────────────────────────────────────────────────────────────
# daily_eod wires fill_pending_entries BEFORE reconcile
# ─────────────────────────────────────────────────────────────────────

class TestDailyEodWiring:
    def test_daily_eod_calls_fill_pending_before_reconcile(self):
        from pathlib import Path
        repo_root = Path(__file__).resolve().parents[2]
        script = repo_root / "backend" / "scripts" / "daily_eod.py"
        assert script.exists()
        src = script.read_text()
        # fill_pending_entries and reconcile_open_trades must both be imported
        assert "fill_pending_entries" in src, (
            "daily_eod.py must import and call fill_pending_entries "
            "(H8 fix)"
        )
        assert "reconcile_open_trades" in src
        # And fill_pending must appear BEFORE reconcile in the source
        # (order-sensitive, since we depend on the H8 fill happening first).
        fp_idx = src.index("fill_pending_entries()")
        rc_idx = src.index("reconcile_open_trades()")
        assert fp_idx < rc_idx, (
            "fill_pending_entries() must be called BEFORE "
            "reconcile_open_trades() in daily_eod.py — otherwise the "
            "same-day reconcile misses newly-filled trades."
        )
