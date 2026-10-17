"""Regression tests for Stage 2B.1 of Session 2 (AUDIT_v2 data-layer fixes).

Guards:
  * C2 — yfinance fundamentals lookahead. The writer must NOT stamp
    tk.info's current-snapshot ratios into historical rows.
  * M1 — Kite tick timezone. Naive timestamps must be localized as IST,
    not UTC, so the stored TIMESTAMPTZ instant is correct.
  * M4 — Cost table dedup. `settings.transaction_costs` must be gone;
    qsde.risk.costs is the single source of truth; the actual values
    reflect the STT correction (delivery = 24 bps realistic / 30 bps paper).

Hermetic — no DB, no network, no yfinance. Uses source inspection + a
monkey-patched yf.Ticker.
"""
from __future__ import annotations

import inspect
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest


# ─────────────────────────────────────────────────────────────────────
# C2 — fundamentals lookahead
# ─────────────────────────────────────────────────────────────────────

class TestC2FundamentalsLookahead:
    """AUDIT_v2 C2: fetch_fundamentals_batch used to copy the CURRENT
    trailing dict into every historical row, poisoning ~15 factor columns
    over the entire training history."""

    def test_writer_no_longer_reads_tk_info(self):
        """The tell for the fix: `tk.info` should not be dereferenced
        inside the historical-row loop of fetch_fundamentals_batch."""
        from qsde.ingestion import yfinance_client
        src = inspect.getsource(yfinance_client.fetch_fundamentals_batch)
        # The function may still reference tk.info in a NEGATIVE comment
        # (which is fine); the assertion is that no code path actually
        # calls .info. Strip comments and check.
        code_only = "\n".join(
            line for line in src.splitlines()
            if not line.strip().startswith("#")
        )
        # `info = tk.info` was the load-bearing line of the C2 bug. It
        # must not reappear.
        assert "info = tk.info" not in code_only, (
            "C2 fix regressed: fetch_fundamentals_batch is reading tk.info "
            "again. That's the current-snapshot poisoner — don't."
        )

    def test_writer_marks_rows_with_pit_v2_source(self):
        """Fixed rows carry source='yfinance_pit_v2' so the DB can tell
        contaminated legacy rows apart from clean new ones."""
        from qsde.ingestion import yfinance_client
        src = inspect.getsource(yfinance_client.fetch_fundamentals_batch)
        assert '"yfinance_pit_v2"' in src, (
            "Fixed writer must tag rows source='yfinance_pit_v2' as the "
            "audit marker for the C2 fix."
        )

    def test_writer_nulls_trailing_columns_explicitly(self):
        """The fix must EXPLICITLY set every current-snapshot column to
        None so ON CONFLICT UPDATE cleans previously-poisoned rows on
        the next sync."""
        from qsde.ingestion import yfinance_client
        src = inspect.getsource(yfinance_client.fetch_fundamentals_batch)
        # Look for the loop that nulls them
        assert "_CONTAMINATED_COLUMNS" in src, (
            "Fixed writer must define and use _CONTAMINATED_COLUMNS to null "
            "every current-snapshot ratio column."
        )
        # Sanity: the tuple must include the load-bearing ones.
        # Read the module-level constant if the writer defines it as a
        # nested constant, we can't reach it — but a substring check works
        # because the writer sets each name to None in a loop.
        for col in ("pe_ratio", "pb_ratio", "roe", "market_cap"):
            assert f'"{col}"' in src, (
                f"Contaminated column '{col}' must be in _CONTAMINATED_COLUMNS"
            )


class TestC2Migration018:
    """AUDIT_v2 C2 companion migration."""

    def test_migration_018_exists_and_nulls_ratios(self):
        from pathlib import Path
        repo_root = Path(__file__).resolve().parents[2]
        mig = repo_root / "infra" / "migrations" / "018_c2_fundamentals_cleanup.sql"
        assert mig.exists(), f"Migration 018 must exist at {mig}"
        sql = mig.read_text()
        # Look at executable SQL only.
        exec_sql = "\n".join(
            line for line in sql.splitlines()
            if not line.lstrip().startswith("--")
        )
        assert "UPDATE fundamentals" in exec_sql
        # Every contaminated column must be nulled by the migration. Use
        # a whitespace-tolerant check: `<col>\s*=\s*NULL` — the migration
        # aligns column names for readability so we can't hard-code spaces.
        import re
        for col in ("pe_ratio", "pb_ratio", "roe", "roa",
                    "gross_margin", "net_margin", "market_cap",
                    "enterprise_value", "dividend_yield"):
            pat = rf"\b{re.escape(col)}\s*=\s*NULL"
            assert re.search(pat, exec_sql), (
                f"Migration 018 must NULL the contaminated column '{col}' "
                f"(no `{col} = NULL` clause found)."
            )
        # And it must retag those rows so audits can distinguish them.
        assert "'yfinance_cleaned_018'" in exec_sql, (
            "Migration 018 must retag cleaned rows with "
            "source='yfinance_cleaned_018' for audit trail."
        )


# ─────────────────────────────────────────────────────────────────────
# M1 — intraday tick timezone
# ─────────────────────────────────────────────────────────────────────

class TestM1IntradayTickTz:
    """AUDIT_v2 M1: _floor_to_minute must localize naive timestamps as
    IST (Asia/Kolkata), not UTC. Kite's exchange_timestamp is naive IST."""

    def test_floor_to_minute_localizes_naive_as_ist(self):
        from qsde.ingestion.intraday_storage import _floor_to_minute
        # 09:15:30 IST on 2026-06-15 — the NSE open bar.
        naive = datetime(2026, 6, 15, 9, 15, 30)
        out = _floor_to_minute(naive)
        # Must be tz-aware.
        assert out.tzinfo is not None, "Result must be tz-aware"
        # And offset must be +05:30 (IST), not +00:00 (UTC).
        offset_hours = out.utcoffset().total_seconds() / 3600
        assert offset_hours == 5.5, (
            f"Naive Kite tick must be localized as IST (+5:30), not "
            f"{offset_hours}h. Storing as UTC shifts every bar by 5.5h."
        )
        # And the wall-clock hour/minute must be preserved.
        assert out.hour == 9 and out.minute == 15
        # Seconds must be floored.
        assert out.second == 0 and out.microsecond == 0

    def test_ist_utc_conversion_gives_correct_instant(self):
        """End-to-end: a 09:15 IST tick must correspond to the SAME instant
        as 03:45 UTC when compared."""
        from qsde.ingestion.intraday_storage import _floor_to_minute
        naive = datetime(2026, 6, 15, 9, 15, 0)
        out = _floor_to_minute(naive)
        # Convert to UTC and check the wall-clock.
        utc = out.astimezone(ZoneInfo("UTC"))
        assert utc.hour == 3 and utc.minute == 45, (
            f"09:15 IST should be 03:45 UTC; got {utc.hour:02d}:{utc.minute:02d}"
        )


# ─────────────────────────────────────────────────────────────────────
# M4 — cost table dedup
# ─────────────────────────────────────────────────────────────────────

class TestM4CostReconciliation:
    """AUDIT_v2 M4: qsde.risk.costs is now the single source of truth.
    settings.transaction_costs is deleted."""

    def test_settings_transaction_costs_is_removed(self):
        from qsde.config import settings
        assert not hasattr(settings, "transaction_costs"), (
            "settings.transaction_costs was a dead property advertising "
            "cost numbers that DISAGREED with the actual cost model. "
            "It has been deleted — do not re-add."
        )

    def test_delivery_cost_reflects_stt_correction(self):
        """AUDIT_v2 M4 called out that 12 bps delivery was wrong — STT
        alone is 20 bps r/t. The fixed value must be at least 24 bps."""
        from qsde.risk.costs import cost_bps
        # Realistic (not paper) — must clear the statutory floor.
        for h in ("swing", "long", "btst"):
            c = cost_bps(h, paper_default=False)
            assert c >= 24.0, (
                f"{h} realistic cost is {c} bps — must be >= 24 bps "
                f"(STT alone is 20 bps r/t; add exchange + GST + stamp)."
            )
        # Paper default must sit above realistic (conservative bias).
        for h in ("swing", "long", "btst"):
            paper = cost_bps(h, paper_default=True)
            real  = cost_bps(h, paper_default=False)
            assert paper >= real, (
                f"{h} paper default ({paper}) must be >= realistic ({real}) "
                f"— paper is intentionally conservative."
            )

    def test_intraday_cost_reflects_mis_regime(self):
        """Intraday MIS: STT only on sell side (2.5 bps) + brokerage cap
        + slippage → ~6 bps realistic, 8 bps paper."""
        from qsde.risk.costs import cost_bps
        real = cost_bps("intraday", paper_default=False)
        paper = cost_bps("intraday", paper_default=True)
        assert 4.0 <= real <= 12.0, (
            f"Intraday realistic cost {real} bps is outside the "
            f"defensible 4–12 bps band."
        )
        assert paper >= real
        # Intraday must be materially cheaper than delivery.
        assert real < cost_bps("swing", paper_default=False)
    def test_cost_bps_is_the_only_public_entry_point(self):
        """Callers must import from qsde.risk.costs, not inline constants
        anywhere. The module docstring must self-identify as canonical."""
        from qsde.risk import costs
        doc = (costs.__doc__ or "").lower()
        assert "single source of truth" in doc, (
            "qsde.risk.costs must document itself as the single source "
            "of truth for cost assumptions."
        )
