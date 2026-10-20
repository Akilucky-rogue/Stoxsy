"""Read queries over the prediction ledger, shared by the Live compare API and the alert engine."""
from __future__ import annotations

import pandas as pd

from qsde.db.connection import read_sql


def load_joined(model_version: str, horizon_id: str) -> pd.DataFrame:
    """Resolved predictions with their outcomes (inner join: an unresolved prediction has no outcome row and is not here)."""
    return read_sql("""SELECT p.*, o.entry_date, o.status, o.exit_date, o.ret_gross, o.ret_net, o.cost_bps, o.excluded, o.data_through
                       FROM prediction_log p JOIN prediction_outcomes o USING (prediction_id) WHERE p.model_version = :m AND p.horizon_id = :h""",
                    params={"m": model_version, "h": horizon_id})


def load_counts(model_version: str, horizon_id: str) -> dict:
    d = read_sql("""SELECT count(*) AS logged, count(o.prediction_id) AS resolved, count(*) FILTER (WHERE o.excluded) AS excluded, count(DISTINCT p.as_of_date) AS dates,
                           min(p.as_of_date) AS first_as_of, max(p.as_of_date) AS last_as_of FROM prediction_log p LEFT JOIN prediction_outcomes o USING (prediction_id)
                    WHERE p.model_version = :m AND p.horizon_id = :h""", params={"m": model_version, "h": horizon_id}).iloc[0]
    return {"logged": int(d["logged"]), "resolved": int(d["resolved"]), "pending": int(d["logged"]) - int(d["resolved"]), "excluded": int(d["excluded"]), "dates": int(d["dates"]),
            "first_as_of": None if pd.isna(d["first_as_of"]) else str(pd.Timestamp(d["first_as_of"]).date()),
            "last_as_of": None if pd.isna(d["last_as_of"]) else str(pd.Timestamp(d["last_as_of"]).date())}


def models_with_predictions() -> pd.DataFrame:
    return read_sql("SELECT model_version, horizon_id, count(*) AS n, max(as_of_date) AS last_as_of FROM prediction_log GROUP BY 1, 2 ORDER BY 1, 2")
