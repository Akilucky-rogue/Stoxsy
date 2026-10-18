"""Owner-approved corporate-action decisions (versioned file, applied on top of the source-confirmed reconciler).

The production reconciler (`source_confirmed_v2`) confirms a listed split/bonus only when the price gap sits within +-2
sessions of the listed date AND the symbol's previous bar is the immediately preceding market session. Real actions on
thinly traded names (halts, T-group stocks) fail that rule and land in SOURCE_UNCONFIRMED. A human can review the
evidence pack (scripts/evidence_packs.py) and approve or reject each one. Decisions live in
`infra/data/corp_action_approvals.json` (in git, so every change is reviewable) and are applied here:

  approve   the SOURCE_UNCONFIRMED row for (symbol, listed date) becomes an applied SPLIT_BONUS / CONSOLIDATION event at
            the OBSERVED gap date, `method = 'owner_approved_v1'`, `confidence = 'high'`. A guard re-measures the gap on
            the data at apply time and refuses to apply if it no longer matches the approved factor.
  reject    the row stays SOURCE_UNCONFIRMED (never applied); the decision and reason are kept for the record.

To undo an approval: delete its entry (or set decision to "reject") and rebuild; the previous snapshot stays on disk.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

APPROVALS_VERSION = "owner_approved_v1"
APPROVALS_FILE = Path(os.getenv("STOXSY_CA_APPROVALS",
                                str(Path(__file__).resolve().parents[3] / "infra" / "data" / "corp_action_approvals.json")))
GUARD_TOL_STRONG = 0.10
GUARD_TOL_MILD = 0.06


def load_approvals(path: Optional[Path] = None) -> list[dict]:
    p = Path(path or APPROVALS_FILE)
    if not p.exists():
        return []
    return json.loads(p.read_text(encoding="utf-8")).get("decisions", [])


def approvals_fingerprint(path: Optional[Path] = None) -> dict:
    p = Path(path or APPROVALS_FILE)
    if not p.exists():
        return {"version": APPROVALS_VERSION, "file": None, "sha256": None, "approved": 0, "rejected": 0}
    d = load_approvals(p)
    return {"version": APPROVALS_VERSION, "file": p.name, "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
            "approved": sum(1 for x in d if x["decision"] == "approve"),
            "rejected": sum(1 for x in d if x["decision"] == "reject")}


def apply_approvals(events: pd.DataFrame, primary: pd.DataFrame, approvals: Optional[list] = None) -> tuple[pd.DataFrame, dict]:
    """Return (events with approved actions applied, report). `primary`: one row per (symbol, date) with open/close."""
    approvals = load_approvals() if approvals is None else approvals
    report = {"applied": [], "rejected": [], "refused": []}
    if not approvals or events is None or events.empty:
        return events, report
    ev = events.copy()
    prim = primary[["symbol", "date", "open", "close"]].copy()
    prim["date"] = pd.to_datetime(prim["date"])
    sessions = np.sort(prim["date"].unique())

    for a in approvals:
        sym, listed = a["symbol"], pd.Timestamp(a["listed_ex_date"])
        near = (ev["symbol"] == sym) & (ev["kind"] == "SOURCE_UNCONFIRMED") & \
               ((pd.to_datetime(ev["ex_date"]) - listed).abs() <= pd.Timedelta(days=12))
        if a["decision"] == "reject":
            report["rejected"].append(f"{sym} {a['listed_ex_date']}")
            continue
        if not near.any():
            report["refused"].append(f"{sym} {a['listed_ex_date']}: no SOURCE_UNCONFIRMED row to approve")
            continue
        g, f = pd.Timestamp(a["gap_date"]), float(a["factor"])
        b = prim[prim["symbol"] == sym].sort_values("date").reset_index(drop=True)
        i = b.index[b["date"] == g]
        if len(i) == 0 or int(i[0]) == 0:
            report["refused"].append(f"{sym}: no bar on approved gap date {a['gap_date']}")
            continue
        i = int(i[0])
        prev_close, op, cl = float(b.loc[i - 1, "close"]), float(b.loc[i, "open"]), float(b.loc[i, "close"])
        ro = op / prev_close
        tol = GUARD_TOL_STRONG if (f <= 0.8 or f >= 1.25) else GUARD_TOL_MILD
        if abs(ro / f - 1.0) > tol:                       # the data moved since the approval was given
            report["refused"].append(f"{sym}: measured gap {ro:.4f} no longer matches approved factor {f:.4f}")
            continue
        base = ev[near].iloc[0]
        skipped = int(((sessions > np.datetime64(b.loc[i - 1, "date"])) & (sessions < np.datetime64(g))).sum())
        listed_pos = int(np.searchsorted(sessions, np.datetime64(listed)))
        gap_pos = int(np.searchsorted(sessions, np.datetime64(g)))
        row = {c: base[c] for c in ev.columns}
        row.update({
            "ex_date": g.date(), "prev_session": b.loc[i - 1, "date"].date(), "close_prev_session": prev_close,
            "open_ex_date": op, "close_ex_date": cl, "open_ratio": ro, "close_ratio": cl / prev_close,
            "kind": "CONSOLIDATION" if f > 1 else "SPLIT_BONUS", "price_factor": f, "applied": True,
            "applied_research": True, "confidence": "high", "method": APPROVALS_VERSION,
            "ex_date_shift": gap_pos - listed_pos,
            "snapped_ratio": f"{base['snapped_ratio']} [owner-approved; {skipped} market sessions untraded before the gap bar]",
        })
        ev = pd.concat([ev[~near], pd.DataFrame([row])], ignore_index=True)
        report["applied"].append(f"{sym} {g.date()} x{f:.4f} (listed {a['listed_ex_date']}, {skipped} untraded sessions before)")
        log.info("approval applied: %s", report["applied"][-1])
    return ev, report
