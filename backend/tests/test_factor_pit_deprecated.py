"""factor_pit is DEPRECATED (migration 026, ADR 0001): no live path may read it.

The Parquet feature store (qsde.features.store) is the only source of features for training, serving and
research. This test fails if a module under api/ or qsde/ issues SQL against factor_pit other than the
allow-listed legacy helpers, so the table can be archived/dropped once the first full-universe snapshot
validates.
"""
from __future__ import annotations

import re
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
READ_SQL = re.compile(r"(FROM|JOIN)\s+factor_pit\b", re.I)

# Legacy helpers that still contain the SQL but have no live caller (verified by test_helpers_have_no_callers).
ALLOWED = {
    "qsde/db/connection.py",          # pit_query / pit_query_bulk (unused by any live path)
    "qsde/factors/pit_writer.py",     # legacy writer: existence probe before expire+insert
}


def _py_files():
    for top in ("api", "qsde"):
        for p in (BACKEND / top).rglob("*.py"):
            yield p


def test_no_live_module_reads_factor_pit():
    offenders = []
    for p in _py_files():
        rel = p.relative_to(BACKEND).as_posix()
        if rel in ALLOWED:
            continue
        if READ_SQL.search(p.read_text(encoding="utf-8", errors="ignore")):
            offenders.append(rel)
    assert not offenders, f"factor_pit is deprecated; read features via qsde.features.store: {offenders}"


def test_helpers_have_no_callers():
    callers = []
    for p in _py_files():
        rel = p.relative_to(BACKEND).as_posix()
        if rel in ALLOWED or rel == "qsde/db/__init__.py":
            continue
        if re.search(r"\bpit_query(_bulk)?\s*\(", p.read_text(encoding="utf-8", errors="ignore")):
            callers.append(rel)
    assert not callers, f"deprecated pit_query helpers still called from: {callers}"


def test_no_live_module_imports_the_legacy_writer():
    """After the analyze.py pin flow moved to the feature store, factor_pit has no writer on any live path."""
    importers = []
    for p in _py_files():
        rel = p.relative_to(BACKEND).as_posix()
        if rel == "qsde/factors/pit_writer.py":
            continue
        if re.search(r"(from|import)s+[w.]*pit_writer|write_factors_to_pit", p.read_text(encoding="utf-8", errors="ignore")):
            importers.append(rel)
    assert not importers, f"legacy factor_pit writer still referenced from: {importers}"
