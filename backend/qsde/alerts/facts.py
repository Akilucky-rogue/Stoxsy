"""Gather the facts the alert rules look at. Each gatherer is independent and may fail on its own: the engine turns a failure into a visible alert instead of hiding it."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from qsde.db.connection import read_sql

DATA = Path(os.environ.get("STOXSY_DATA_DIR", r"D:\Stoxsy\data"))
SIGNAL = DATA / "signals" / "defensive" / "latest.parquet"
ACTION_RE = r"split|sub-division|subdivision|bonus|rights|demerger|de-merger|merger|amalgamation|spin|capital reduction|consolidation"


def _signal() -> Optional[pd.DataFrame]:
    if not SIGNAL.exists():
        return None
    s = pd.read_parquet(SIGNAL)
    s["date"] = pd.to_datetime(s["date"])
    return s


def data_dates() -> dict:
    d = read_sql("SELECT (SELECT max(date) FROM eod_raw WHERE exchange = 'NSE') AS nse, (SELECT max(date) FROM bse_eod_raw) AS bse, (SELECT max(date) FROM mf_nav) AS amfi, (SELECT max(trade_date) FROM fno_eod_sessions) AS fno").iloc[0]
    return {"nse_last": None if pd.isna(d["nse"]) else pd.Timestamp(d["nse"]).date(), "bse_last": None if pd.isna(d["bse"]) else pd.Timestamp(d["bse"]).date(),
            "amfi_last": None if pd.isna(d["amfi"]) else pd.Timestamp(d["amfi"]).date(),
            "fno_last": None if pd.isna(d["fno"]) else pd.Timestamp(d["fno"]).date()}


def sources() -> list:
    return read_sql("SELECT source, status, consecutive_failures, last_error, last_success_at FROM source_health").to_dict("records")


def corp_actions(cfg: dict, now: pd.Timestamp) -> dict:
    c = cfg["corporate_actions"]
    n = read_sql("""SELECT (SELECT count(*) FROM corp_action_events WHERE applied_research AND NOT applied) AS research_only,
                           (SELECT count(*) FROM corp_action_candidates WHERE classification IN ('unresolved', 'v3_candidate')) AS cands""").iloc[0]
    out = {"unreviewed_research_only": int(n["research_only"]), "candidates_open": int(n["cands"]), "upcoming": []}
    sig = _signal()
    if sig is not None and len(sig):
        today = now.tz_convert("Asia/Kolkata").normalize().tz_localize(None)
        up = read_sql("""SELECT DISTINCT symbol, ex_date, subject FROM nse_corporate_actions WHERE symbol = ANY(:s) AND ex_date BETWEEN CAST(:a AS DATE) AND CAST(:b AS DATE)
                         AND subject ~* :re ORDER BY ex_date, symbol""", params={"s": sig["symbol"].tolist(), "a": str((today - pd.Timedelta(days=c["upcoming_window_days_before"])).date()),
                                                                               "b": str((today + pd.Timedelta(days=c["upcoming_window_days_after"])).date()), "re": ACTION_RE})
        out["upcoming"] = [{"symbol": r.symbol, "ex_date": str(pd.Timestamp(r.ex_date).date()), "subject": str(r.subject)[:160]} for r in up.itertuples()]
    return out


def models() -> list:
    from qsde.live_loop import view
    from qsde.live_loop.queries import load_counts, load_joined, models_with_predictions

    out = []
    for r in models_with_predictions().itertuples():
        c = load_counts(r.model_version, r.horizon_id)
        h = view.health(load_joined(r.model_version, r.horizon_id)) if c["resolved"] else {}
        out.append({"model_version": r.model_version, "horizon_id": r.horizon_id, "counts": c, "health": h})
    return out


def logging_status() -> list:
    """Is the latest defensive signal in the prediction ledger, and by when must it be? (The shadow model is the only forward logger today.)"""
    from qsde.live_loop import ledger
    from qsde.live_loop.cards_log import MODEL_VERSION

    sig = _signal()
    if sig is None or sig.empty:
        return []
    sd = pd.Timestamp(sig["date"].iloc[0]).normalize()
    last = read_sql("SELECT max(as_of_date) AS d FROM prediction_log WHERE model_version = :m", params={"m": MODEL_VERSION})["d"].iloc[0]
    return [{"model_version": MODEL_VERSION, "signal_date": str(sd.date()), "last_logged": None if pd.isna(last) else str(pd.Timestamp(last).date()),
             "deadline": ledger.entry_deadline_ist(sd).isoformat()}]


def kite() -> dict:
    d = read_sql("SELECT count(*) AS n, max(expires_at) AS exp FROM kite_tokens WHERE is_active")
    n = int(d["n"].iloc[0])
    return {"has_token": n > 0, "expires_at": None if n == 0 or pd.isna(d["exp"].iloc[0]) else pd.Timestamp(d["exp"].iloc[0])}


def portfolio(user_id: str, cfg: dict) -> Optional[dict]:
    """The user's latest import: weights, average pairwise correlation, pro-forma drawdown, share in the highest-risk defensive buckets, regime."""
    from qsde.ml import data as mldata
    from qsde.portfolio import analytics

    imp = read_sql("SELECT id FROM portfolio_imports WHERE user_id = :u ORDER BY imported_at DESC LIMIT 1", params={"u": user_id})
    if imp.empty:
        return None
    h = read_sql("SELECT symbol, quantity::float8 AS quantity, avg_cost::float8 AS avg_cost FROM portfolio_holdings WHERE import_id = :i", params={"i": int(imp["id"].iloc[0])})
    c = cfg["portfolio"]
    px = mldata.load_prices(h["symbol"].tolist(), start=str((pd.Timestamp.today() - pd.Timedelta(days=max(400, int(c["correlation_lookback_sessions"] * 1.6)))).date()))[["symbol", "date", "close"]]
    if px.empty:
        return None
    last = px.sort_values("date").groupby("symbol").tail(1)
    df = analytics.value_holdings(h, last, px["date"].max())
    wts = df.dropna(subset=["weight"]).set_index("symbol")["weight"]
    wide = px.pivot(index="date", columns="symbol", values="close").sort_index()
    rets = wide.pct_change().tail(int(c["correlation_lookback_sessions"])).dropna(axis=1, thresh=int(0.8 * c["correlation_lookback_sessions"]))
    corr, pair = None, None
    if rets.shape[1] >= c["min_names_for_correlation"]:
        cm = rets.corr()
        iu = np.triu_indices(len(cm), 1)
        corr = float(np.nanmean(cm.to_numpy()[iu]))
        k = int(np.nanargmax(cm.to_numpy()[iu]))
        pair = (cm.index[iu[0][k]], cm.columns[iu[1][k]], float(cm.to_numpy()[iu][k]))
    hist = analytics.history(h, wide, None, 365)
    sig = _signal()
    hr = reg = None
    if sig is not None:
        prof = analytics.defensive_profile(df, sig)
        hr = float(sum(prof["buckets"].get(b, {}).get("share", 0.0) for b in ("1", "2")))
        reg = str(sig["regime"].iloc[0])
    return {"weights": {k: float(v) for k, v in wts.items()}, "avg_pairwise_correlation": corr, "top_pair": pair, "current_drawdown": None if hist is None else hist["current_drawdown"],
            "high_risk_share": hr, "regime": reg}


def series_consistency() -> dict:
    """The official `ohlcv` series (events flagged applied) versus the governed registry (`adjustment_events_applied`), keyed on symbol and NSE ex-date."""
    d = read_sql("""WITH a AS (SELECT symbol, ex_date FROM corp_action_events WHERE applied),
                         r AS (SELECT symbol, nse_ex_date AS ex_date FROM adjustment_events_applied)
                    SELECT (SELECT count(*) FROM a) AS applied, (SELECT count(*) FROM r) AS registry,
                           (SELECT count(*) FROM a WHERE NOT EXISTS (SELECT 1 FROM r WHERE r.symbol = a.symbol AND r.ex_date = a.ex_date)) AS a_not_r,
                           (SELECT count(*) FROM r WHERE NOT EXISTS (SELECT 1 FROM a WHERE a.symbol = r.symbol AND a.ex_date = r.ex_date)) AS r_not_a""").iloc[0]
    return {"applied_in_ohlcv": int(d["applied"]), "registry_events": int(d["registry"]), "applied_not_in_registry": int(d["a_not_r"]), "registry_not_applied": int(d["r_not_a"])}


def bse_corporate_actions() -> dict:
    d = read_sql("SELECT (SELECT count(*) FROM bse_ca_files) AS files, (SELECT max(loaded_at) FROM bse_ca_files) AS last_loaded, (SELECT count(*) FROM bse_corporate_actions) AS actions").iloc[0]
    return {"files": int(d["files"]), "actions": int(d["actions"]), "last_loaded": None if pd.isna(d["last_loaded"]) else pd.Timestamp(d["last_loaded"])}


def bucket_a() -> dict:
    from qsde.ingestion import bucket_a_decisions as BA

    pr = BA.progress()
    return {**pr, "fingerprint": BA.fingerprint()}
