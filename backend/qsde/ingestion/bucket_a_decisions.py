"""Owner adjudication of Bucket A events (status `official_match_price_mismatch`): the NSE record matches type, ratio, ex-date and ISIN, but the observed price gap differs from the
listed factor by 3-10%, outside the hard bounds of the `owner_approved` path (`corp_actions_approvals.check_bounds`, which this module never weakens).

Decisions live in `infra/data/corp_action_bucket_a_decisions.json` (in git: every change is reviewable). A decision is RECORDED here and takes effect only at the single, versioned rebuild of the
official price series that happens after ALL events are adjudicated. All 69 are adjudicated and the series was rebuilt under the owner's policy (2026-10-25), so `ohlcv` is labelled
`official_prices_bucket_a_policy_v1`: confirmed events keep the factor, HELD events keep it provisionally (flagged applied_unreviewed, NOT validated), rejected events lose it.

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
from qsde.config.paths import data_path

VERSION = "bucket_a_decisions_v1"
FILE = Path(os.getenv("STOXSY_BUCKET_A_DECISIONS", str(Path(__file__).resolve().parents[3] / "infra" / "data" / "corp_action_bucket_a_decisions.json")))
WORKSHEET = data_path("reports", "corp_actions", "review_bucket_a_official_match_price_mismatch.csv")
DECISIONS = ("confirm", "needs_more_evidence", "reject")
SERIES_LABELS = {"ohlcv": "official_prices_bucket_a_policy_v1", "sbadj_v1": "governed_research_series_not_production"}


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


# ── the policy as an input to the rebuild ────────────────────────────────────────────────────────────────────────────────────────────────

HELD_KEEP = "keep_listed_factor_flagged_unreviewed"
REVIEW_STATUS = {"confirm": "owner_confirmed", "needs_more_evidence": "applied_unreviewed", "reject": "adjustment_removed"}


def apply_policy(events: pd.DataFrame, d: Optional[dict] = None) -> tuple[pd.DataFrame, dict]:
    """Apply the owner's Bucket A decisions to the corporate-action events BEFORE the official series is built.

    `corp_action_events` is a derived table that is rebuilt whole on every run, so the decisions must be an input to the rebuild (a one-off edit would be undone by the next
    daily run). Per the owner's rebuild policy (Option B, 2026-10-23): CONFIRMED events keep their listed factor, tagged owner_confirmed; HELD events keep it too, tagged
    applied_unreviewed (provisional, NOT validated); only REJECTED events lose their adjustment (applied and applied_research both off, tagged adjustment_removed).
    Returns (events with review_status / review_note columns, report). A decision that matches no event is reported, never ignored."""
    d = d or load()
    ev = events.copy()
    ev["review_status"] = None
    ev["review_note"] = None
    pol = d.get("rebuild_policy", {})
    keep_held = pol.get("held", HELD_KEEP) == HELD_KEEP
    if ev.empty:
        return ev, {"matched": 0, "unmatched": [(x["symbol"], x["ex_date"]) for x in d["decisions"]], "changed": []}
    keys = list(zip(ev["symbol"], pd.to_datetime(ev["ex_date"]).dt.strftime("%Y-%m-%d")))
    index = {}
    for i, k in enumerate(keys):
        index.setdefault(k, []).append(i)
    unmatched, changed, counts = [], [], {"owner_confirmed": 0, "applied_unreviewed": 0, "adjustment_removed": 0}
    for x in d["decisions"]:
        rows = index.get((x["symbol"], x["ex_date"]))
        if not rows:
            unmatched.append((x["symbol"], x["ex_date"]))
            continue
        status = REVIEW_STATUS[x["decision"]]
        for i in rows:
            ev.iat[i, ev.columns.get_loc("review_status")] = status
            ev.iat[i, ev.columns.get_loc("review_note")] = x["reason"]
            if x["decision"] == "reject" or (x["decision"] == "needs_more_evidence" and not keep_held):
                if bool(ev.iat[i, ev.columns.get_loc("applied")]) or bool(ev.iat[i, ev.columns.get_loc("applied_research")]):
                    changed.append((x["symbol"], x["ex_date"], x["decision"]))
                ev.iat[i, ev.columns.get_loc("applied")] = False
                ev.iat[i, ev.columns.get_loc("applied_research")] = False
                if x["decision"] != "reject":
                    ev.iat[i, ev.columns.get_loc("review_status")] = "adjustment_removed"
        counts[ev.iat[rows[0], ev.columns.get_loc("review_status")]] = counts.get(ev.iat[rows[0], ev.columns.get_loc("review_status")], 0) + 1
    return ev, {"matched": len(d["decisions"]) - len(unmatched), "unmatched": unmatched, "changed": changed, "counts": counts, "held_policy": "keep" if keep_held else "remove"}


def review_queue(read_sql=None, worksheet: Optional[Path] = None) -> dict:
    """What still needs evidence (the held events, applied provisionally) and what the owner rejected, for the product to show. Held events are sorted by universe impact first
    (in the top-500 universe within 60 days, best rank first), then by the size of the unexplained price gap."""
    from qsde.db.connection import read_sql as _rs

    rs = read_sql or _rs
    d = load()
    ws = worksheet or WORKSHEET
    extra = {}
    if ws.exists():
        w = pd.read_csv(ws)
        for r in w.itertuples():
            extra[(r.symbol, str(pd.Timestamp(r.nse_ex_date).date()))] = {"residual": float(r.residual_overnight_after_listed_factor), "sigma": float(r.residual_in_sigma),
                                                                          "in_universe": bool(r.in_universe_within_60d), "best_rank": None if pd.isna(r.best_universe_rank) else int(r.best_universe_rank),
                                                                          "listed_factor": float(r.listed_factor)}
    held, rejected = [], []
    for x in d["decisions"]:
        e = extra.get((x["symbol"], x["ex_date"]), {})
        row = {"symbol": x["symbol"], "ex_date": x["ex_date"], "reason": x["reason"], **e}
        if x["decision"] == "needs_more_evidence":
            held.append(row)
        elif x["decision"] == "reject":
            rejected.append(row)
    held.sort(key=lambda r: (not r.get("in_universe", False), r.get("best_rank") or 10**6, -abs(r.get("residual") or 0)))
    for r in rejected:
        g = rs("""WITH d AS (SELECT date, open, close, lag(close) OVER (ORDER BY date) AS pc FROM ohlcv WHERE symbol = :s AND date BETWEEN CAST(:t AS DATE) - 10 AND CAST(:t AS DATE))
                  SELECT open / pc - 1 AS gap FROM d WHERE date = CAST(:t AS DATE)""", params={"s": r["symbol"], "t": r["ex_date"]})
        r["raw_gap"] = None if g.empty or pd.isna(g["gap"].iloc[0]) else float(g["gap"].iloc[0])
    return {"policy": d.get("rebuild_policy", {}), "held": held, "rejected": rejected,
            "note": "Held events are provisionally applied under the owner's policy (price error up to about 10%) and are NOT validated; rejected events show the raw corporate-action move."}
