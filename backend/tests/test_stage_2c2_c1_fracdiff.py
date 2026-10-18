"""Regression tests for AUDIT_v2 C1 (fracdiff train/serve unification).

History: the first fix (Stage 2C.2) differenced factors at ingest, tagged
`ffd_v1`, and pinned the intent with source-text greps. Phase 1 found the fix
was ineffective in the EOD path (duplicate-label reindex swallowed per column,
and only a ~10-day tail was differenced with a different kernel), so the
version was bumped to `ffd_v2` and these tests now assert BEHAVIOUR and the
shared FFD_VERSION constant rather than literal strings. Behavioural coverage
of the skew itself lives in test_phase1_pipeline_repairs.py.

Hermetic: no DB, no network.
"""
from __future__ import annotations

import inspect
from pathlib import Path

import numpy as np
import pandas as pd


class TestPitWriterAcceptsFactorVersion:
    def test_writer_signature_has_factor_version(self):
        from qsde.factors.pit_writer import write_factors_to_pit
        sig = inspect.signature(write_factors_to_pit)
        assert "factor_version" in sig.parameters

    def test_writer_default_version_is_raw(self):
        """The writer's default is the honest RAW tag; the engine must opt in
        to the differenced version explicitly."""
        from qsde.factors.pit_writer import write_factors_to_pit
        from qsde.factors.versions import RAW_VERSION
        sig = inspect.signature(write_factors_to_pit)
        assert sig.parameters["factor_version"].default == RAW_VERSION

    def test_insert_sql_includes_factor_version_column(self):
        from qsde.factors import pit_writer
        src = inspect.getsource(pit_writer.write_factors_to_pit)
        assert "factor_version)" in src or "factor_version," in src


class TestFactorEngineDifferencesAtIngest:
    def test_engine_tags_writes_with_ffd_version_and_actually_transforms(self, monkeypatch):
        """The engine must write FFD_VERSION rows whose level-form columns are
        really differenced (the first fix silently left them raw)."""
        from qsde.factors import engine
        from qsde.factors.versions import FFD_VERSION

        idx = pd.bdate_range("2020-01-01", periods=700, name="date")
        rng = np.random.default_rng(5)
        level = np.cumsum(rng.normal(size=700)) + 500.0
        frame = pd.DataFrame({"symbol": "AAA", "tech_obv_slope": level,
                              "tech_rsi_14": rng.uniform(20, 80, 700)}, index=idx)
        captured: dict = {}
        monkeypatch.setattr(engine, "compute_factors_for_symbol", lambda s, start="": frame.copy())
        monkeypatch.setattr(engine, "_persist",
                            lambda df, version: captured.update(df=df.copy(), k={"factor_version": version}))
        engine.compute_factors_batch(["AAA"])

        assert captured["k"]["factor_version"] == FFD_VERSION
        wrote = captured["df"].dropna(subset=["tech_obv_slope"])
        assert len(wrote) > 100
        # Level was ~500; a differenced series is far smaller in magnitude.
        assert wrote["tech_obv_slope"].abs().max() < 100
        # Stationary column untouched.
        np.testing.assert_allclose(captured["df"]["tech_rsi_14"].to_numpy(),
                                   frame["tech_rsi_14"].to_numpy())

    def test_failure_to_difference_falls_back_to_raw_tag(self, monkeypatch):
        from qsde.factors import engine
        from qsde.factors.versions import RAW_VERSION

        idx = pd.bdate_range("2020-01-01", periods=300, name="date")
        frame = pd.DataFrame({"symbol": "AAA", "tech_obv_slope": np.arange(300.0)}, index=idx)
        captured: dict = {}
        monkeypatch.setattr(engine, "compute_factors_for_symbol", lambda s, start="": frame.copy())
        monkeypatch.setattr(engine, "to_ffd_frame",
                            lambda df: (_ for _ in ()).throw(RuntimeError("boom")))
        monkeypatch.setattr(engine, "_persist",
                            lambda df, version: captured.update(k={"factor_version": version}))
        engine.compute_factors_batch(["AAA"])
        assert captured["k"]["factor_version"] == RAW_VERSION


class TestTrainAndServeReadTheSameVersion:
    def test_dataset_default_is_the_shared_constant(self):
        from qsde.factors.versions import FFD_VERSION
        from qsde.models.dataset import build_training_dataset
        sig = inspect.signature(build_training_dataset)
        assert sig.parameters["factor_version"].default == FFD_VERSION
        assert sig.parameters["apply_fracdiff"].default is False

    def test_dataset_and_serve_use_the_same_reader_module(self):
        from qsde.models import dataset, signal_generator
        from qsde.ml import dataset as ml_dataset
        d_src = Path(ml_dataset.__file__).read_text()
        s_src = Path(signal_generator.__file__).read_text()
        assert "read_features" in d_src and "build_labelled_dataset" in Path(dataset.__file__).read_text()
        assert "qsde.features.store import read_latest_rows" in s_src
        assert "FFD_VERSION" in s_src, "serve must use the shared version constant"
        # No hard-coded version literals left to drift.
        assert "'ffd_v" not in s_src and '"ffd_v' not in s_src

    def test_reader_is_pinned_to_the_version_it_is_asked_for(self, tmp_path):
        """A version's directory is its own namespace: reading ffd_v2 can never
        return rows written under another version."""
        from datetime import datetime, timezone

        from qsde.features.store import SnapshotWriter, read_features

        idx = pd.bdate_range("2026-01-05", periods=5, name="date")
        for ver, val in (("ffd_v2", 2.0), ("raw_v0", 9.0)):
            w = SnapshotWriter(ver, "u", root=tmp_path, fingerprint={}, sha="s",
                               built_at=datetime(2026, 10, 1, tzinfo=timezone.utc))
            w.add(pd.DataFrame({"symbol": "AAA", "f": val}, index=idx))
            w.close(register=False)
        got = read_features("ffd_v2", root=tmp_path)
        assert (got["f"] == 2.0).all()

    def test_retired_and_raw_versions_are_not_the_read_version(self):
        from qsde.factors.versions import FFD_VERSION, RAW_VERSION
        assert FFD_VERSION not in {RAW_VERSION, "ffd_v1"}


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
        assert "DEFAULT 'raw_v0'" in exec_sql
        assert "CREATE INDEX" in exec_sql and "factor_version" in exec_sql
