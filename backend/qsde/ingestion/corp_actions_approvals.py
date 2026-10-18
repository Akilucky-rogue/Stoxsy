"""Owner-approved corporate-action decisions (versioned file, applied on top of the source-confirmed reconciler).

The production reconciler (`source_confirmed_v2`) confirms a listed split/bonus only when the price gap sits within +-2
sessions of the listed date AND the symbol's previous bar is the immediately preceding market session. Real actions on
thinly traded names (halts, T-group stocks) fail that rule and land in SOURCE_UNCONFIRMED. A human can review the
evidence pack (scripts/evidence_packs.py) and approve or reject each one. Decisions live in
`infra/data/corp_action_approvals.json` (in git, so every change is reviewable) and are applied here:

  approve   the SOURCE_UNCONFIRMED row for (symbol, listed date) becomes an applied SPLIT_BONUS / CONSOLIDATION event at
            the OBSERVED gap date, `method = 'owner_approved_v1'`, `confidence = 'high'`. A guard re-measures the gap on
            the data at apply time and refuses to apply if it no longer matches the approved factor.
  reject / rejected_pending_evidence
            the row stays SOURCE_UNCONFIRMED (never applied); the decision and reason are kept for the record.
  Hard bounds (check_bounds) apply at decision time and again at apply time: gap bar within 5 market sessions of the listed
  ex-date, factor within 5% (3% for mild factors), at most 5 untraded sessions before the gap bar.
  `confidence` ("high" default, or "medium") is stored on the event.

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
# HARD BOUNDS (owner decision 2026-10-18, after NDRAUTO: gap 25 sessions after the listing, factor 7.8% off). An approval is
# only valid if the listing and the observed price behaviour agree closely AND promptly. The same bounds are checked when an
# evidence pack is produced (decision time) and again here (apply time).
MAX_SHIFT_SESSIONS = 5           # market sessions between the listed ex-date and the observed gap bar
MAX_UNTRADED_SESSIONS = 5        # market sessions the symbol did not trade before the gap bar
FACTOR_TOL_STRONG = 0.05         # |observed gap / listed factor - 1| for factors <= 0.8 or >= 1.25
FACTOR_TOL_MILD = 0.03           # milder factors barely clear noise, so the match must be tighter
NOT_APPLIED_DECISIONS = ("reject", "rejected_pending_evidence", "manual_review", "proposed")
OFFICIAL_TIER = "official_confirmed"
EVIDENCE_DIR = Path(os.getenv("STOXSY_EVIDENCE_DIR", r"D:\Stoxsy\data\reports\corp_actions\evidence"))
CA_API_URL = "https://www.nseindia.com/api/corporates-corporateActions"


def check_bounds(factor: float, observed_ratio: float, shift_sessions: int, untraded_sessions: int) -> list:
    """Violations of the hard bounds (empty list = within bounds)."""
    out = []
    tol = FACTOR_TOL_STRONG if (factor <= 0.8 or factor >= 1.25) else FACTOR_TOL_MILD
    err = abs(observed_ratio / factor - 1.0)
    if err > tol:
        out.append(f"factor error {err:.1%} > {tol:.0%}")
    if abs(shift_sessions) > MAX_SHIFT_SESSIONS:
        out.append(f"gap bar is {abs(shift_sessions)} market sessions from the listed ex-date (> {MAX_SHIFT_SESSIONS})")
    if untraded_sessions > MAX_UNTRADED_SESSIONS:
        out.append(f"{untraded_sessions} untraded sessions before the gap bar (> {MAX_UNTRADED_SESSIONS})")
    return out


def rename_reachable(renames: Optional[pd.DataFrame], old: str, new: str, depth: int = 4) -> bool:
    """True if `old` -> ... -> `new` through OFFICIAL NSE symbol changes (renames: old_symbol, new_symbol)."""
    if renames is None or renames.empty:
        return False
    nxt: dict = {}
    for r in renames.itertuples():
        nxt.setdefault(r.old_symbol, set()).add(r.new_symbol)
    frontier, seen = {old}, {old}
    for _ in range(depth):
        frontier = {n for o in frontier for n in nxt.get(o, ())} - seen
        if new in frontier:
            return True
        seen |= frontier
    return False


def verify_official(a: dict, nse: Optional[pd.DataFrame], renames: Optional[pd.DataFrame], prev_bar: pd.Timestamp,
                    gap: pd.Timestamp, evidence_dir: Optional[Path]):
    """The mandatory conditions of the `official_confirmed` tier that are not price-factor bounds.

    (1) an NSE record for the SAME symbol with the same action type and an equal parsed ratio near the listed date;
    (2) its ex-date lies after the stock's last pre-gap bar and on/before the gap bar (on the gap bar, or inside the untraded window);
    (3) continuity from the traded symbol to the symbol NSE keys the action under is explained by an OFFICIAL rename;
    (4) the evidence pack referenced by the approval exists.
    Returns (reasons_for_refusal, record)."""
    why, rec = [], None
    f = float(a["factor"])
    if nse is None or nse.empty:
        return ["no NSE records supplied to verify against"], None
    c = nse[(nse["symbol"] == a["symbol"]) & nse["category"].isin(["SPLIT_BONUS", "CONSOLIDATION"]) & nse["factor"].notna()]
    c = c[((c["factor"].astype(float) / f) - 1.0).abs() < 1e-6]
    c = c[(pd.to_datetime(c["ex_date"]) - pd.Timestamp(a["listed_ex_date"])).abs() <= pd.Timedelta(days=12)]
    if c.empty:
        why.append("no NSE record with the same symbol, action type and ratio near the listed date")
    else:
        rec = c.sort_values("known_at").iloc[0]
        ex = pd.Timestamp(rec["ex_date"])
        if not (prev_bar < ex <= gap):
            why.append(f"official ex-date {ex.date()} is not inside ({prev_bar.date()}, {gap.date()}]")
    traded = a.get("apply_to_symbol", a["symbol"])
    if traded != a["symbol"] and not rename_reachable(renames, traded, a["symbol"]):
        why.append(f"{traded} -> {a['symbol']} is not an official NSE rename")
    if evidence_dir is not None:
        pack = a.get("evidence_pack")
        if not pack or not (Path(evidence_dir) / pack).exists():
            why.append(f"evidence pack {pack!r} not found")
    return why, rec


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
            "rejected": sum(1 for x in d if x["decision"] in NOT_APPLIED_DECISIONS)}


def apply_approvals(events: pd.DataFrame, primary: pd.DataFrame, approvals: Optional[list] = None,
                    nse_records: Optional[pd.DataFrame] = None, renames: Optional[pd.DataFrame] = None,
                    evidence_dir: Optional[Path] = EVIDENCE_DIR) -> tuple[pd.DataFrame, dict]:
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
        if a["decision"] in NOT_APPLIED_DECISIONS:
            report["rejected"].append(f"{sym} {a['listed_ex_date']}")
            continue
        if not near.any():
            report["refused"].append(f"{sym} {a['listed_ex_date']}: no SOURCE_UNCONFIRMED row to approve")
            continue
        g, f = pd.Timestamp(a["gap_date"]), float(a["factor"])
        b = prim[prim["symbol"] == a.get("apply_to_symbol", sym)].sort_values("date").reset_index(drop=True)
        i = b.index[b["date"] == g]
        if len(i) == 0 or int(i[0]) == 0:
            report["refused"].append(f"{sym}: no bar on approved gap date {a['gap_date']}")
            continue
        i = int(i[0])
        prev_close, op, cl = float(b.loc[i - 1, "close"]), float(b.loc[i, "open"]), float(b.loc[i, "close"])
        ro = op / prev_close
        base = ev[near].iloc[0]
        skipped = int(((sessions > np.datetime64(b.loc[i - 1, "date"])) & (sessions < np.datetime64(g))).sum())
        official, rec = a.get("tier") == OFFICIAL_TIER, None
        if official:                                          # second tier: the NSE record corroborates, bounds still apply
            why, rec = verify_official(a, nse_records, renames, b.loc[i - 1, "date"], g, evidence_dir)
            if why:
                report["refused"].append(f"{sym} {a['listed_ex_date']}: official_confirmed conditions failed - " + "; ".join(why))
                continue
        # timing is measured from the OFFICIAL ex-date under the second tier, from the listed date otherwise
        ref_date = pd.Timestamp(rec["ex_date"]) if official else listed
        listed_pos = int(np.searchsorted(sessions, np.datetime64(ref_date)))
        gap_pos = int(np.searchsorted(sessions, np.datetime64(g)))
        bad = check_bounds(f, ro, gap_pos - listed_pos, skipped)      # re-measured on the data as it is NOW
        if bad:
            report["refused"].append(f"{sym} {a['listed_ex_date']}: outside the hard bounds - " + "; ".join(bad))
            continue
        row = {c: base[c] for c in ev.columns}
        target_sym = a.get("apply_to_symbol", sym)              # renamed stocks: the action belongs to the symbol that traded
        if target_sym != sym:
            row["symbol"] = target_sym
            # a price-inferred (research-only) event for the same gap is superseded by this source-backed one
            dup = (ev["symbol"] == target_sym) & (ev["source"] == "price_inferred") &                   ((pd.to_datetime(ev["ex_date"]) - g).abs() <= pd.Timedelta(days=4))
            ev = ev[~dup]
            near = near.reindex(ev.index, fill_value=False) if len(dup) and dup.any() else near
        row.update({
            "ex_date": g.date(), "prev_session": b.loc[i - 1, "date"].date(), "close_prev_session": prev_close,
            "open_ex_date": op, "close_ex_date": cl, "open_ratio": ro, "close_ratio": cl / prev_close,
            "kind": "CONSOLIDATION" if f > 1 else "SPLIT_BONUS", "price_factor": f, "applied": True,
            "applied_research": True, "confidence": OFFICIAL_TIER if official else a.get("confidence", "high"),
            "method": APPROVALS_VERSION, "approval_tier": OFFICIAL_TIER if official else None,
            "isin": (rec["isin"] if official else None), "source_url": (CA_API_URL if official else None),
            "retrieved_at": (rec["known_at"] if official else None), "evidence_ref": (a.get("evidence_pack") if official else None),
            "ex_date_shift": gap_pos - listed_pos,
            "snapped_ratio": f"{base['snapped_ratio']} [{'official_confirmed' if official else 'owner-approved'}; "
                             f"{skipped} market sessions untraded before the gap bar]",
        })
        ev = pd.concat([ev[~near], pd.DataFrame([row])], ignore_index=True)
        report["applied"].append(f"{sym} {g.date()} x{f:.4f} (listed {a['listed_ex_date']}, {skipped} untraded sessions before)")
        log.info("approval applied: %s", report["applied"][-1])
    return ev, report
