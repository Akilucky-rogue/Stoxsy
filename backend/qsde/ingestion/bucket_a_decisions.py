"""Owner adjudication of Bucket A events (status `official_match_price_mismatch`): the NSE record matches type, ratio, ex-date and ISIN, but the observed price gap differs from the
listed factor by 3-10%, outside the hard bounds of the `owner_approved` path (`corp_actions_approvals.check_bounds`, which this module never weakens).

Decisions live in `infra/data/corp_action_bucket_a_decisions.json` (in git: every change is reviewable). A decision is RECORDED here and takes effect only at the single, versioned rebuild of the
official price series that happens after ALL events are adjudicated; until then `ohlcv` is unchanged and labelled `official_prices_with_pending_corp_action_review`.

  confirm                the listed NSE factor is accepted for this event despite the price residual (the owner has judged the residual an ordinary move)
  needs_more_evidence    held: no decision yet; the event stays unconfirmed in the rebuilt series until evidence is added
  reject                 the listed factor is not accepted for this event
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Optional

import pandas as pd

VERSION = "bucket_a_decisions_v1"
FILE = Path(os.getenv("STOXSY_BUCKET_A_DECISIONS", str(Path(__file__).resolve().parents[3] / "infra" / "data" / "corp_action_bucket_a_decisions.json")))
WORKSHEET = Path(r"D:\Stoxsy\data\reports\corp_actions\review_bucket_a_official_match_price_mismatch.csv")
DECISIONS = ("confirm", "needs_more_evidence", "reject")
SERIES_LABELS = {"ohlcv": "official_prices_with_pending_corp_action_review", "sbadj_v1": "governed_research_series_not_production"}


class DecisionError(ValueError):
    pass


def load(path: Path = FILE) -> dict:
    d = json.loads(path.read_text(encoding="utf-8"))
    seen: set = set()
    for x in d.get("decisions", []):
        if x.get("decision") not in DECISIONS:
            raise DecisionError(f"{x.get('symbol')} {x.get('ex_date')}: decision must be one of {DECISIONS}")
        key = (x["symbol"], x["ex_date"])
        if key in seen:
            raise DecisionError(f"duplicate decision for {key}")
        seen.add(key)
        for f in ("symbol", "ex_date", "decided_by", "decided_at", "reason"):
            if not x.get(f):
                raise DecisionError(f"{key}: missing {f}")
    return d


def fingerprint(path: Path = FILE) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def worksheet_keys(path: Path = WORKSHEET) -> Optional[set]:
    if not path.exists():
        return None
    w = pd.read_csv(path)
    return {(r.symbol, str(pd.Timestamp(r.nse_ex_date).date())) for r in w.itertuples()}


def progress(d: Optional[dict] = None, keys: Optional[set] = None) -> dict:
    """{total, confirm, needs_more_evidence, reject, pending}: pending = worksheet events with no decision. Decisions that match no worksheet event are reported as unknown."""
    d = d or load()
    keys = worksheet_keys() if keys is None else keys
    dec = {(x["symbol"], x["ex_date"]): x["decision"] for x in d["decisions"]}
    total = len(keys) if keys is not None else None
    unknown = [k for k in dec if keys is not None and k not in keys]
    counts = {c: sum(1 for v in dec.values() if v == c) for c in DECISIONS}
    return {"total": total, **counts, "decided": len(dec), "pending": None if total is None else total - len([k for k in dec if k in keys]), "unknown": unknown}
