"""Regression tests for Stage 2C.2 of Session 2 (AUDIT_v2 C1 — fracdiff
train/serve unification).

C1 bug: dataset.py fracdiff'd factor values before training; signal_generator
served raw levels from factor_pit. Every level-form feature was in different
units at serve vs train time. Tree splits learned on fracdiff'd magnitudes
routed raw serve values into degenerate leaves.

Fix (option a per audit): apply fracdiff at INGEST time (factors/engine.py),
tag rows with `factor_version='ffd_v1'`, and filter both dataset.py and
signal_generator.py to read only that version. Train and serve read the
same transformed values — no more skew.

Hermetic — no DB, no network. Uses source inspection + signature checks.
"""
from __future__ import annotations

import inspect
from pathlib import Path

import pytest


class TestPitWriterAcceptsFactorVersion:
    def test_writer_signature_has_factor_version(self):
        from qsde.factors.pit_writer import write_factors_to_pit
        sig = inspect.signature(write_factors_to_pit)
        assert "factor_version" in sig.parameters, (
            "C1 fix requires write_factors_to_pit(factor_version=...) "
            "so callers can tag rows as raw_v0 or ffd_v1."
        )

    def test_writer_default_version_is_raw_v0(self):
        """Default is 'raw_v0' — honest tag for pre-fracdiff writes. The
        factor engine must OPT IN to ffd_v1 explicitly."""
        from qsde.factors.pit_writer import write_factors_to_pit
        sig = inspect.signature(write_factors_to_pit)
        assert sig.parameters["factor_version"].default == "raw_v0"

    def test_insert_sql_includes_factor_version_column(self):
        """Source-level guard: the INSERT must include the new column."""
        from qsde.factors import pit_writer
        src = inspect.getsource(pit_writer.write_factors_to_pit)
        assert "factor_version" in src
        # And the INSERT must reference it in the column list.
        assert "factor_version)" in src or "factor_version," in src, (
            "INSERT INTO factor_pit column list must include factor_version"
        )


class TestFactorEngineAppliesFracdiffAtIngest:
    def test_engine_applies_fracdiff_before_write(self):
        """Source-level guard: qsde/factors/engine.py must call
        apply_fracdiff_to_features before write_factors_to_pit."""
        from qsde.factors import engine
        src = Path(engine.__file__).read_text()
        # Both must be present, and fracdiff must appear BEFORE the write.
        assert "apply_fracdiff_to_features" in src, (
            "engine.py must import/use apply_fracdiff_to_features (C1 fix)"
        )
        assert "write_factors_to_pit" in src
        fd_idx = src.index("apply_fracdiff_to_features")
        # Look for the LAST write_factors_to_pit call (the real one, not import)
        write_idx = src.rindex("write_factors_to_pit(to_write")
        assert fd_idx < write_idx, (
            "engine.py must call apply_fracdiff_to_features BEFORE "
            "write_factors_to_pit — otherwise the C1 fix is not in effect."
        )

    def test_engine_tags_writes_as_ffd_v1(self):
        """Source-level guard: the engine must write with factor_version='ffd_v1'."""
        from qsde.factors import engine
        src = Path(engine.__file__).read_text()
        assert '"ffd_v1"' in src or "'ffd_v1'" in src, (
            "engine.py must tag writes as factor_version='ffd_v1' (C1 fix)"
        )


class TestDatasetReadsFilteredByFactorVersion:
    def test_default_apply_fracdiff_is_false(self):
        """C1 fix moves fracdiff to ingest — dataset must NOT double-apply.
        Default is False."""
        from qsde.models.dataset import build_training_dataset
        sig = inspect.signature(build_training_dataset)
        assert sig.parameters["apply_fracdiff"].default is False, (
            "Post-C1-fix, build_training_dataset's apply_fracdiff default "
            "must be False — the values in factor_pit are already fracdiff'd "
            "when written by the engine."
        )

    def test_dataset_accepts_factor_version_param(self):
        from qsde.models.dataset import build_training_dataset
        sig = inspect.signature(build_training_dataset)
        assert "factor_version" in sig.parameters
        assert sig.parameters["factor_version"].default == "ffd_v1", (
            "Default read version must be 'ffd_v1' — otherwise dataset "
            "silently reads pre-fix raw_v0 rows and the C1 fix is defeated."
        )

    def test_dataset_query_filters_by_factor_version(self):
        """Source-level guard: the factor_pit SELECT must include a
        `factor_version = :fv` filter."""
        from qsde.models import dataset
        src = Path(dataset.__file__).read_text()
        # The SELECT block should include the filter.
        assert "factor_version = :fv" in src or \
               "factor_version = :factor_version" in src, (
            "dataset.py's factor_pit SELECT must filter by factor_version — "
            "otherwise training silently pools raw + ffd rows."
        )


class TestSignalGeneratorReadsFilteredByFactorVersion:
    def test_serve_query_filters_by_ffd_v1(self):
        """Source-level guard: signal_generator's SELECT must filter to
        factor_version='ffd_v1' so serve reads the same distribution as
        training. THIS IS THE C1 FIX."""
        from qsde.models import signal_generator
        src = Path(signal_generator.__file__).read_text()
        assert "factor_version = 'ffd_v1'" in src, (
            "signal_generator.py must filter factor_pit to "
            "factor_version = 'ffd_v1' — this is the load-bearing part "
            "of the C1 fix. Without it, live serve reads raw levels while "
            "the model expects fracdiff'd inputs."
        )


class TestMigration021:
    def test_migration_021_adds_factor_version_column(self):
        repo_root = Path(__file__).resolve().parents[2]
        mig = repo_root / "infra" / "migrations" / "021_factor_version.sql"
        assert mig.exists(), f"Migration 021 must exist at {mig}"
        sql = mig.read_text()
        exec_sql = "\n".join(
            line for line in sql.splitlines()
            if not line.lstrip().startswith("--")
        )
        assert "ALTER TABLE factor_pit" in exec_sql
        assert "ADD COLUMN IF NOT EXISTS factor_version" in exec_sql
        assert "DEFAULT 'raw_v0'" in exec_sql, (
            "Migration 021 must default existing rows to 'raw_v0' — "
            "they were written pre-fix and are honestly RAW."
        )
        # And an index on the version column for reader performance.
        assert "CREATE INDEX" in exec_sql and "factor_version" in exec_sql, (
            "Migration 021 must create an index on factor_version so the "
            "reader filter is cheap on 87M+ rows."
        )


class TestC1LoadBearingProperty:
    """The single load-bearing test: source-level check that BOTH the
    train path AND the serve path filter to the SAME factor_version, so
    they can't diverge."""

    def test_train_and_serve_read_same_version(self):
        from qsde.models import dataset, signal_generator
        d_src = Path(dataset.__file__).read_text()
        s_src = Path(signal_generator.__file__).read_text()
        # Train default:
        assert 'factor_version: str = "ffd_v1"' in d_src or \
               "factor_version: str = 'ffd_v1'" in d_src, (
            "Training path must default to reading ffd_v1 rows."
        )
        # Serve hard-codes ffd_v1 (no runtime override — safer):
        assert "factor_version = 'ffd_v1'" in s_src, (
            "Serve path must hard-code ffd_v1 — otherwise a config drift "
            "silently reintroduces the C1 skew."
        )
