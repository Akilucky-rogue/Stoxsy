"""Immutable prediction ledger: what a model said, before the outcome existed.

`prepare()` is pure (validation, ids, hashes). `append_predictions()` writes through migration 037's append-only table.
Rules enforced here, not left to callers:
  * a prediction is keyed by (model_version, symbol, as_of_date, horizon, side); logging the same key again with IDENTICAL
    content is a no-op, with DIFFERENT content it raises ImmutableViolation (a changed model must carry a new model_version)
  * the decision is taken at the close of `as_of_date`: a prediction timestamped before that session's close (15:30 IST) used an
    incomplete bar and is rejected as a point-in-time violation
  * the label definition (label_id) comes from the registry, not the caller
"""
from __future__ import annotations

import hashlib
import json
import math
from typing import Optional

import numpy as np
import pandas as pd

from qsde.ml.registry import get_horizon

IST = "Asia/Kolkata"
CLOSE_HHMM = (15, 30)

REQUIRED = ("model_version", "symbol", "as_of_date", "prediction_time", "horizon_id", "side", "score")
OPTIONAL = ("model_card_id", "security_id", "exchange", "ref_price", "sigma", "rank_pct", "p_win", "expected_return", "ci_low",
            "ci_high", "position_size", "regime", "snapshot_id", "factor_version", "universe_version", "feature_hash",
            "ca_source_hash", "news_refs", "git_sha", "params")
HASHED = ("model_version", "model_card_id", "symbol", "security_id", "exchange", "as_of_date", "prediction_time", "known_at",
          "horizon_id", "label_id", "side", "ref_price", "sigma", "score", "rank_pct", "p_win", "expected_return", "ci_low",
          "ci_high", "position_size", "regime", "snapshot_id", "factor_version", "universe_version", "feature_hash",
          "ca_source_hash", "news_refs", "git_sha", "params")
TABLE_COLS = ("prediction_id", *HASHED, "content_hash")


class PITViolation(ValueError):
    """The prediction claims to have been made before the data it depends on existed."""


class ImmutableViolation(ValueError):
    """The same prediction key was logged again with different content."""


def prediction_id(model_version: str, symbol: str, as_of_date, horizon_id: str, side: int) -> str:
    key = "|".join([str(model_version), str(symbol), pd.Timestamp(as_of_date).strftime("%Y-%m-%d"), str(horizon_id), str(int(side))])
    return hashlib.sha256(key.encode()).hexdigest()[:32]


def _canon(v):
    if v is None or (isinstance(v, float) and not math.isfinite(v)) or (not isinstance(v, (str, dict, list)) and pd.isna(v)):
        return None
    if isinstance(v, (pd.Timestamp,)):
        return v.tz_convert("UTC").isoformat() if v.tzinfo is not None else v.isoformat()
    if isinstance(v, (np.floating, float)):
        return float(f"{float(v):.12g}")
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (dict, list)):
        return json.loads(json.dumps(v, sort_keys=True, default=str))
    return v if not hasattr(v, "isoformat") else v.isoformat()


def content_hash(row: dict) -> str:
    payload = {k: _canon(row.get(k)) for k in HASHED}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def session_close_ist(d) -> pd.Timestamp:
    t = pd.Timestamp(d).normalize()
    return pd.Timestamp(year=t.year, month=t.month, day=t.day, hour=CLOSE_HHMM[0], minute=CLOSE_HHMM[1], tz=IST)


OPEN_HHMM = (9, 15)


def entry_deadline_ist(as_of_date) -> pd.Timestamp:
    """Latest moment a prediction for `as_of_date` may be logged: the open of the next WEEKDAY session. Exchange holidays make the true entry later, never earlier,
    so this is the safe (stricter) bound: a prediction logged after it could have seen the entry price."""
    t = pd.Timestamp(as_of_date).normalize() + pd.Timedelta(days=1)
    while t.dayofweek >= 5:
        t += pd.Timedelta(days=1)
    return pd.Timestamp(year=t.year, month=t.month, day=t.day, hour=OPEN_HHMM[0], minute=OPEN_HHMM[1], tz=IST)


def prepare(df: pd.DataFrame, now: Optional[pd.Timestamp] = None, enforce_pre_entry: bool = False) -> pd.DataFrame:
    """Validate and stamp ids/hashes. Pure: no database access. With enforce_pre_entry, a prediction stamped at or after the next session's open is
    rejected: the entry price (the open of t+1) would already be known, so it is not a prediction."""
    missing = [c for c in REQUIRED if c not in df.columns]
    if missing:
        raise ValueError(f"prediction frame is missing columns: {missing}")
    d = df.copy()
    for c in OPTIONAL:
        if c not in d.columns:
            d[c] = None
    d["exchange"] = d["exchange"].fillna("NSE")
    d["as_of_date"] = pd.to_datetime(d["as_of_date"]).dt.normalize()
    pt = pd.to_datetime(d["prediction_time"], utc=True)
    d["prediction_time"] = pt
    d["known_at"] = pt                                      # the system learns the prediction when it makes it
    if now is not None:
        now_utc = pd.Timestamp(now)
        now_utc = now_utc.tz_localize("UTC") if now_utc.tzinfo is None else now_utc.tz_convert("UTC")
        if (pt > now_utc).any():
            raise PITViolation("prediction_time lies in the future")
    close = d["as_of_date"].map(lambda x: session_close_ist(x).tz_convert("UTC"))
    early = pt < close
    if early.any():
        bad = d.loc[early, ["symbol", "as_of_date"]].head(3).to_dict("records")
        raise PITViolation(f"{int(early.sum())} predictions were stamped before the close of their as_of_date, e.g. {bad}")
    if enforce_pre_entry:
        deadline = d["as_of_date"].map(lambda x: entry_deadline_ist(x).tz_convert("UTC"))
        late = pt >= deadline
        if late.any():
            bad = d.loc[late, ["symbol", "as_of_date"]].head(3).to_dict("records")
            raise PITViolation(f"{int(late.sum())} predictions were stamped after the next session's open (the entry price was already known), e.g. {bad}")
    if not np.isfinite(d["score"].astype(float)).all():
        raise ValueError("non-finite score")
    if not d["side"].isin([-1, 1]).all():
        raise ValueError("side must be +1 or -1")
    for c in ("p_win", "rank_pct"):
        v = pd.to_numeric(d[c], errors="coerce")
        if ((v < 0) | (v > 1)).any():
            raise ValueError(f"{c} must lie in [0, 1]")
    lo, hi = pd.to_numeric(d["ci_low"], errors="coerce"), pd.to_numeric(d["ci_high"], errors="coerce")
    if (lo > hi).any():
        raise ValueError("ci_low > ci_high")
    d["label_id"] = d["horizon_id"].map(lambda h: get_horizon(h).label)         # the definition comes from the registry
    d["prediction_id"] = [prediction_id(r.model_version, r.symbol, r.as_of_date, r.horizon_id, r.side) for r in d.itertuples()]
    if d["prediction_id"].duplicated().any():
        raise ImmutableViolation("duplicate prediction keys inside one batch")
    d["content_hash"] = [content_hash(r) for r in d.to_dict("records")]
    return d[list(TABLE_COLS)]


def split_by_existing(prepared: pd.DataFrame, existing: dict[str, str]) -> tuple[pd.DataFrame, int]:
    """(new rows, number of identical re-logs). Raises ImmutableViolation if any existing key has a different content hash."""
    ids = prepared["prediction_id"]
    seen = ids.isin(existing.keys())
    same = prepared[seen].apply(lambda r: existing[r["prediction_id"]] == r["content_hash"], axis=1) if seen.any() else pd.Series(dtype=bool)
    if len(same) and not same.all():
        bad = prepared[seen][~same.values]["prediction_id"].head(3).tolist()
        raise ImmutableViolation(f"{int((~same).sum())} predictions already logged with different content, e.g. {bad}")
    return prepared[~seen], int(seen.sum())


STAMPS = ("prediction_time", "known_at")


def restamped_hash(row: dict, stored: dict) -> str:
    """The hash this row would have had if it had been stamped when the stored one was: only the two stamp times are swapped for the stored values."""
    return content_hash({**row, **{k: stored[k] for k in STAMPS}})


def _forgive_restamps(p: pd.DataFrame, existing: dict) -> pd.DataFrame:
    """A same-day re-run of the logger stamps new times and so hashes differently, although the prediction itself is identical. Such rows are treated as identical re-logs (the FIRST stamp is
    kept in the table); a row whose content really differs still reaches `split_by_existing` and is refused."""
    mism = p[p["prediction_id"].isin(existing.keys()) & (p["prediction_id"].map(existing) != p["content_hash"])]
    if mism.empty:
        return p
    from qsde.db.connection import read_sql

    st = read_sql(f"SELECT prediction_id, {', '.join(HASHED)} FROM prediction_log WHERE prediction_id = ANY(:ids)", params={"ids": mism["prediction_id"].tolist()})
    st["prediction_id"] = st["prediction_id"].str.strip()
    stored = {r["prediction_id"]: r for r in st.to_dict("records")}
    p = p.copy()
    for i, r in mism.to_dict("index").items():
        s = stored.get(r["prediction_id"])
        if s is not None and restamped_hash(r, s) == existing[r["prediction_id"]]:
            p.loc[i, "content_hash"] = existing[r["prediction_id"]]
    return p


# ── database ─────────────────────────────────────────────────────────

def append_predictions(df: pd.DataFrame, now: Optional[pd.Timestamp] = None, enforce_pre_entry: bool = True) -> dict:
    import psycopg2.extras

    from qsde.db.connection import get_sync_conn, read_sql

    p = prepare(df, now=now, enforce_pre_entry=enforce_pre_entry)
    ex = read_sql("SELECT prediction_id, content_hash FROM prediction_log WHERE prediction_id = ANY(:ids)",
                  params={"ids": p["prediction_id"].tolist()})
    existing = dict(zip(ex["prediction_id"].str.strip(), ex["content_hash"].str.strip()))
    p = _forgive_restamps(p, existing)
    new, dup = split_by_existing(p, existing)
    if len(new):
        cols = list(TABLE_COLS)
        sql = f"INSERT INTO prediction_log ({', '.join(cols)}) VALUES ({', '.join('%(' + c + ')s' for c in cols)}) ON CONFLICT DO NOTHING"
        recs = []
        for r in new.to_dict("records"):
            r = {k: (None if (v is None or (not isinstance(v, (dict, list, str)) and pd.isna(v))) else v) for k, v in r.items()}
            for k in ("news_refs", "params"):
                r[k] = None if r[k] is None else psycopg2.extras.Json(r[k])
            for k in ("prediction_time", "known_at"):
                r[k] = r[k].to_pydatetime()
            r["as_of_date"] = r["as_of_date"].date()
            r["side"] = int(r["side"])
            recs.append(r)
        with get_sync_conn() as conn:
            psycopg2.extras.execute_batch(conn.cursor(), sql, recs, page_size=500)
    return {"logged": int(len(new)), "identical_relogs": dup}
