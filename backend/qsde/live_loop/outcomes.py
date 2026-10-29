"""Outcome joiner: resolve logged predictions with the SAME label builder training used (tb_nextopen_v1 via `make_labels`).

The joiner never sees a bar after `data_through` and only returns predictions whose full window t+1..t+h already exists, so an
outcome cannot be computed from information that arrived after the prediction's horizon resolved - and an unresolved prediction
simply has no outcome row. Entry is the open of t+1, barriers are `entry * (1 -/+ mult * sigma)`, ties are stop-first, gaps fill at
the open: identical to training, the paper journal and the Scorecard (one barrier walker).
"""
from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

from qsde.ml.labels import make_labels

OUTCOME_COLS = ("prediction_id", "data_through", "entry_date", "entry_price", "stop_price", "target_price", "status", "exit_date",
                "exit_price", "gap_exit", "ret_gross", "ret_net", "cost_bps", "excluded", "sigma_check")


def resolve(pred: pd.DataFrame, prices: pd.DataFrame, data_through: Optional[pd.Timestamp] = None,
            unexplained: Optional[pd.DataFrame] = None) -> pd.DataFrame:
    """pred: prediction_id, symbol, as_of_date, horizon_id, side[, sigma]. prices: symbol,date,open,high,low,close (adjusted).

    Returns one row per RESOLVABLE prediction (OUTCOME_COLS). Predictions whose window is incomplete are omitted."""
    px = prices.copy()
    px["date"] = pd.to_datetime(px["date"])
    through = pd.Timestamp(data_through) if data_through is not None else px["date"].max()
    px = px[px["date"] <= through]                                        # nothing after the cut-off may influence an outcome
    out = []
    for (hid, side), g in pred.groupby(["horizon_id", "side"], sort=False):
        ev = g[["symbol", "as_of_date"]].rename(columns={"as_of_date": "date"}).drop_duplicates()
        lab = make_labels(px, ev, hid, side=int(side), unexplained=unexplained, drop_contaminated=False, keep_paths=False)
        if lab.empty:
            continue
        if "sigma" not in g.columns:
            g = g.assign(sigma=np.nan)
        m = g.merge(lab, on=["symbol", "as_of_date"], how="inner", suffixes=("", "_lab"))
        sig_log = pd.to_numeric(m["sigma"], errors="coerce")
        sig_new = m["sigma_lab"]
        excl = (m["touches_unexplained"] | m["feature_contaminated"]) if {"touches_unexplained", "feature_contaminated"} <= set(m.columns) else False
        out.append(pd.DataFrame({
            "prediction_id": m["prediction_id"], "data_through": through.normalize(), "entry_date": m["entry_date"],
            "entry_price": m["entry"], "stop_price": m["stop"], "target_price": m["target"], "status": m["status"],
            "exit_date": m["exit_date"], "exit_price": m["exit_price"], "gap_exit": m["gap_exit"], "ret_gross": m["ret_gross"],
            "ret_net": m["ret_net"], "cost_bps": m["cost_bps"], "excluded": excl,
            "sigma_check": (sig_log / sig_new - 1.0).abs()}))
    if not out:
        return pd.DataFrame(columns=list(OUTCOME_COLS))
    return pd.concat(out, ignore_index=True)[list(OUTCOME_COLS)]


def join_outcomes(pred: pd.DataFrame, outcomes: pd.DataFrame) -> pd.DataFrame:
    """Predictions with their outcomes (inner: only resolved ones). Used by the monitor."""
    return pred.merge(outcomes, on="prediction_id", how="inner")


def unresolved(pred: pd.DataFrame, outcomes: pd.DataFrame) -> pd.DataFrame:
    return pred[~pred["prediction_id"].isin(outcomes["prediction_id"])]


# ── database ─────────────────────────────────────────────────────────

def resolve_pending(prices: pd.DataFrame, data_through: Optional[pd.Timestamp] = None, limit: int = 100000) -> dict:
    """Resolve every logged prediction that has no outcome yet and whose window is complete; write outcomes once."""
    import psycopg2.extras

    from qsde.db.connection import get_sync_conn, read_sql

    pend = read_sql("""SELECT p.prediction_id, p.symbol, p.as_of_date, p.horizon_id, p.side, p.sigma FROM prediction_log p
                       LEFT JOIN prediction_outcomes o USING (prediction_id) WHERE o.prediction_id IS NULL
                       ORDER BY p.as_of_date LIMIT :n""", params={"n": limit})
    if pend.empty:
        return {"pending": 0, "resolved": 0}
    pend["prediction_id"] = pend["prediction_id"].str.strip()
    pend["as_of_date"] = pd.to_datetime(pend["as_of_date"])
    oc = resolve(pend, prices, data_through)
    if len(oc):
        cols = list(OUTCOME_COLS)
        sql = (f"INSERT INTO prediction_outcomes ({', '.join(cols)}) VALUES ({', '.join('%(' + c + ')s' for c in cols)}) "
               f"ON CONFLICT DO NOTHING")
        recs = []
        for r in oc.to_dict("records"):
            r = {k: (None if (not isinstance(v, (str, bool)) and pd.isna(v)) else v) for k, v in r.items()}
            for k in ("data_through", "entry_date", "exit_date"):
                r[k] = None if r[k] is None else pd.Timestamp(r[k]).date()
            r["excluded"] = bool(r["excluded"])
            r["gap_exit"] = None if r["gap_exit"] is None else bool(r["gap_exit"])
            recs.append(r)
        with get_sync_conn() as conn:
            psycopg2.extras.execute_batch(conn.cursor(), sql, recs, page_size=500)
    return {"pending": int(len(pend)), "resolved": int(len(oc))}
