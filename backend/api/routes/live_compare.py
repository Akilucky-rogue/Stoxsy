"""Live compare: what every model predicted, what the market then did, and whether the evidence says the model is working.

Strictly read-only: there is no endpoint that writes, edits or deletes a prediction or an outcome (the ledger is append-only in the database as well). Predictions are
shown exactly as logged; an outcome appears only after its barrier window has resolved; a model's state is `research`, `paper` or `approved`, never "guaranteed".
"""
from __future__ import annotations

from typing import Optional

import pandas as pd
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from qsde.db.connection import read_sql
from qsde.live_loop import view
from qsde.live_loop.queries import load_counts, load_joined
from qsde.live_loop.monitor import Thresholds

router = APIRouter()
DISCLAIMER = ("Research and decision support, not investment advice. Outcomes are mechanical (the same barrier walker and cost model used in training); "
              "nothing here is guaranteed and no model in this system has been shown to have a net edge.")


class ModelRow(BaseModel):
    model_version: str
    state: str
    description: Optional[str] = None
    predictions: int
    resolved: int
    pending: int
    first_as_of: Optional[str] = None
    last_as_of: Optional[str] = None
    horizons: list[str]


class ModelList(BaseModel):
    models: list[ModelRow]
    ledger_rules: list[str]
    disclaimer: str = DISCLAIMER


RULES = ["Predictions are append-only: they cannot be edited or deleted, and a changed model must carry a new model_version.",
         "A prediction must be stamped after the close of its as-of session and before the next session's open (the entry price must not be known yet).",
         "An outcome is computed only after its full window has elapsed, by the same barrier walker and cost model used in training, and is shown only then.",
         "A model that logs no probability or expected return is not graded against one."]


@router.get("/live/models", response_model=ModelList)
def models():
    d = read_sql("""SELECT p.model_version, count(*) AS predictions, count(o.prediction_id) AS resolved, min(p.as_of_date) AS first_as_of, max(p.as_of_date) AS last_as_of,
                           array_agg(DISTINCT p.horizon_id) AS horizons FROM prediction_log p LEFT JOIN prediction_outcomes o USING (prediction_id)
                    GROUP BY 1 ORDER BY max(p.as_of_date) DESC""")
    reg = view.model_registry()
    rows = [ModelRow(model_version=r.model_version, state=view.model_state(r.model_version, reg), description=(reg.get(r.model_version) or {}).get("description"), predictions=int(r.predictions),
                     resolved=int(r.resolved), pending=int(r.predictions) - int(r.resolved), first_as_of=str(pd.Timestamp(r.first_as_of).date()), last_as_of=str(pd.Timestamp(r.last_as_of).date()),
                     horizons=sorted(r.horizons)) for r in d.itertuples()]
    known = {r.model_version for r in rows}
    for mv, meta in reg.items():                                    # a registered model with nothing logged yet still appears, honestly empty
        if mv not in known:
            rows.append(ModelRow(model_version=mv, state=view.model_state(mv, reg), description=meta.get("description"), predictions=0, resolved=0, pending=0, horizons=[]))
    return ModelList(models=rows, ledger_rules=RULES)


class Summary(BaseModel):
    model_version: str
    horizon_id: str
    state: str
    counts: dict
    health: dict
    thresholds: dict
    notes: list[str]
    disclaimer: str = DISCLAIMER


@router.get("/live/summary", response_model=Summary)
def summary(model_version: str = Query(..., max_length=120), horizon_id: str = Query("position_20d", pattern="^(intraday_1d|swing_5d|swing_10d|position_20d)$")):
    counts = load_counts(model_version, horizon_id)
    if counts["logged"] == 0 and model_version not in view.model_registry():
        raise HTTPException(status_code=404, detail="no predictions logged for this model version")
    h = view.health(load_joined(model_version, horizon_id) if counts["resolved"] else pd.DataFrame())
    notes = []
    if counts["logged"] == 0:
        notes.append("nothing logged yet: run scripts/log_cards.py after the end-of-day load and before the next session opens")
    elif counts["resolved"] == 0:
        notes.append(f"{counts['logged']:,} predictions are waiting for their windows to elapse; the first outcomes appear {view.expected_window(counts['first_as_of'], horizon_id)['exit_by']} (estimate) or later")
    th = Thresholds()
    return Summary(model_version=model_version, horizon_id=horizon_id, state=view.model_state(model_version), counts=counts, health=h, notes=notes,
                   thresholds={"min_resolved": th.min_resolved, "min_dates": th.min_dates, "kill_slope_low": th.kill_slope_low, "kill_drawdown_cum": th.kill_drawdown_cum,
                               "slope_band": [th.slope_low, th.slope_high], "psi_feature": th.psi_feature})


class PredictionList(BaseModel):
    n_matching: int
    offset: int
    rows: list[dict]
    disclaimer: str = DISCLAIMER


def load_predictions(model_version: str, horizon_id: Optional[str], state: str, symbol: Optional[str], as_of: Optional[str], selected_only: bool, limit: int, offset: int) -> tuple[int, pd.DataFrame, pd.DataFrame]:
    where, params = ["p.model_version = :m"], {"m": model_version}
    if horizon_id:
        where.append("p.horizon_id = :h")
        params["h"] = horizon_id
    if symbol:
        where.append("p.symbol = :s")
        params["s"] = symbol.upper()
    if as_of:
        where.append("p.as_of_date = CAST(:d AS DATE)")
        params["d"] = as_of
    if selected_only:
        where.append("p.position_size > 0")
    if state == "pending":
        where.append("o.prediction_id IS NULL")
    elif state == "resolved":
        where.append("o.prediction_id IS NOT NULL")
    w = " AND ".join(where)
    frm = "FROM prediction_log p LEFT JOIN prediction_outcomes o USING (prediction_id)"
    n = int(read_sql(f"SELECT count(*) AS n {frm} WHERE {w}", params=params)["n"].iloc[0])
    pred = read_sql(f"SELECT p.* {frm} WHERE {w} ORDER BY p.as_of_date DESC, p.position_size DESC NULLS LAST, p.rank_pct DESC NULLS LAST, p.symbol LIMIT :lim OFFSET :off",
                    params={**params, "lim": limit, "off": offset})
    oc = read_sql("SELECT * FROM prediction_outcomes WHERE prediction_id = ANY(:ids)", params={"ids": pred["prediction_id"].tolist()}) if len(pred) else pd.DataFrame()
    return n, pred, oc


@router.get("/live/predictions", response_model=PredictionList)
def predictions(model_version: str = Query(..., max_length=120), horizon_id: Optional[str] = Query(None, pattern="^(intraday_1d|swing_5d|swing_10d|position_20d)$"),
                state: str = Query("all", pattern="^(all|pending|resolved)$"), symbol: Optional[str] = Query(None, max_length=20, pattern="^[A-Za-z0-9&._-]+$"),
                as_of: Optional[str] = Query(None, pattern="^\\d{4}-\\d{2}-\\d{2}$"), selected_only: bool = False, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0, le=1_000_000)):
    n, pred, oc = load_predictions(model_version, horizon_id, state, symbol, as_of, selected_only, limit, offset)
    by = {} if oc.empty else {str(r["prediction_id"]).strip(): r for r in oc.to_dict("records")}
    reg = view.model_registry()
    rows = [view.shape_prediction(p, by.get(str(p["prediction_id"]).strip()), reg) for p in pred.to_dict("records")]
    return PredictionList(n_matching=n, offset=offset, rows=rows)
