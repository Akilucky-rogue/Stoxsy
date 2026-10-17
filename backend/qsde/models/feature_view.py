"""Shared reader for point-in-time factors, used by BOTH training and serving.

Keeping the SQL and the long->wide reshape in one module is what stops the
two paths drifting apart (AUDIT C1/M2). Only `factor_version = FFD_VERSION`
rows are ever read.
"""
from __future__ import annotations

from typing import Sequence

import pandas as pd

# Training: all active rows in a date window. Bind params: :s, :e, :fv.
# Filters belong in WHERE; ORDER BY must be last (it was not, in the broken
# version). DISTINCT ON picks one row per key; valid_from DESC makes that
# deterministic if a correction ever left two active rows.
FACTOR_PIT_TRAIN_SQL = """
SELECT DISTINCT ON (symbol, as_of_date, factor_name)
       symbol, as_of_date, factor_name, factor_value
  FROM factor_pit
 WHERE as_of_date >= CAST(:s AS DATE)
   AND as_of_date <= CAST(:e AS DATE)
   AND factor_version = :fv
   AND valid_to = 'infinity'::timestamptz
 ORDER BY symbol, as_of_date, factor_name, valid_from DESC
"""

# Serving: newest active value per (symbol, factor) in a recent window.
# Bind param: :fv. (Mixed-vintage risk across the 20-day window is tracked
# as AUDIT M2 and addressed with the as-of serving view in a later phase.)
FACTOR_PIT_SERVE_SQL = """
SELECT DISTINCT ON (symbol, factor_name)
       symbol, factor_name, factor_value
  FROM factor_pit
 WHERE valid_to = 'infinity'::timestamptz
   AND factor_version = :fv
   AND as_of_date >= (CURRENT_DATE - INTERVAL '20 days')
 ORDER BY symbol, factor_name, as_of_date DESC
"""


def pivot_factors(long_df: pd.DataFrame, index: Sequence[str]) -> pd.DataFrame:
    """Long (factor_name, factor_value) rows -> one wide row per `index` key.

    `aggfunc="last"` so a duplicate key can never be silently averaged.
    """
    return long_df.pivot_table(
        index=list(index),
        columns="factor_name",
        values="factor_value",
        aggfunc="last",
    ).reset_index()
