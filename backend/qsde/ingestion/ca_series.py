"""Which corporate-action truth each price series carries, stated once and used everywhere (cards, signal lineage, alerts, System page).

`ohlcv` is the series every consumer reads. Until all Bucket A events are adjudicated and the series is rebuilt once from the reconciled registry it is labelled
`official_prices_with_pending_corp_action_review`; the governed `sbadj_v1` registry is `governed_research_series_not_production` (nothing in production reads it).
"""
from __future__ import annotations

import hashlib

from qsde.ingestion.bucket_a_decisions import SERIES_LABELS

OHLCV_LABEL = SERIES_LABELS["ohlcv"]
REGISTRY_LABEL = SERIES_LABELS["sbadj_v1"]


def applied_events_fingerprint() -> str:
    """Fingerprint of the corporate-action events applied in `ohlcv` (symbol, ex-date, price factor): it changes whenever the official series would change."""
    from qsde.db.connection import read_sql

    e = read_sql("SELECT symbol, ex_date, price_factor FROM corp_action_events WHERE applied ORDER BY symbol, ex_date, price_factor")
    payload = "|".join(f"{r.symbol}:{r.ex_date}:{float(r.price_factor):.10g}" for r in e.itertuples())
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def lineage_tag() -> str:
    return f"ohlcv[{OHLCV_LABEL}]:{applied_events_fingerprint()}"
