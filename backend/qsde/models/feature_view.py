"""Compatibility shim. The feature matrix now lives in the Parquet store.

Training (`qsde.models.dataset`) and serving (`qsde.models.signal_generator`)
both import the reader from `qsde.features.store`. The old long-form SQL
constants (`FACTOR_PIT_TRAIN_SQL` / `FACTOR_PIT_SERVE_SQL`) were removed with the
switch so that no second store can silently diverge (ADR 0001).
"""
from qsde.features.store import (  # noqa: F401
    feature_columns, latest_factor, read_features, read_latest_rows,
)
