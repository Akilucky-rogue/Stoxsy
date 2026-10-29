"""Apply the owner's TIERED approval policy to the v3 candidates and record the decisions in the approvals file.

Policy (owner, 2026-10-18):
  approve        official NSE rename + gap ON the listed ex-date + 0 untraded sessions + factor error <= 3%
  manual_review  same, but factor error 3% - 5% (never auto-approved; needs individual evidence review)
  (anything with a halt, timing mismatch or ambiguous hand-off was already rejected by the v3 evaluator's hard bounds)

Only `approve` entries become applied events. Existing entries in the file are never overwritten; manual_review entries are
recorded so the owner sees them, and are NOT applied.

    python scripts/promote_v3_proposals.py [--max-error 0.03]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

FILE = Path(__file__).resolve().parents[2] / "infra" / "data" / "corp_action_approvals.json"


def tier(r, max_error: float) -> str:
    if r.shift_sessions != 0 or r.untraded_sessions != 0:
        return "reject"
    if r.factor_error <= max_error:
        return "approve"
    return "manual_review"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-error", type=float, default=0.03)
    args = ap.parse_args()
    from qsde.db.connection import read_sql

    cand = read_sql("SELECT * FROM corp_action_candidates WHERE reconciler_version = 'source_confirmed_v3' "
                    "AND classification = 'v3_candidate' ORDER BY listed_symbol, listed_ex_date")
    doc = json.loads(FILE.read_text(encoding="utf-8"))
    have = {(d["symbol"], d["listed_ex_date"], d.get("apply_to_symbol")) for d in doc["decisions"]}
    counts = {"approve": 0, "manual_review": 0, "reject": 0, "skipped_existing": 0}
    for r in cand.itertuples():
        key = (r.listed_symbol, str(r.listed_ex_date), r.symbol_of_day)
        t = tier(r, args.max_error)
        if t == "reject":
            counts["reject"] += 1
            continue
        if key in have:
            counts["skipped_existing"] += 1
            continue
        doc["decisions"].append({
            "symbol": r.listed_symbol, "apply_to_symbol": r.symbol_of_day, "listed_ex_date": str(r.listed_ex_date),
            "gap_date": str(r.gap_date), "factor": round(float(r.listed_factor), 10), "decision": t,
            "decided_by": "owner (tiered policy)", "decided_at": "2026-10-18",
            "notes": (f"Official NSE rename; gap on the listed date, 0 untraded sessions, factor error {r.factor_error:.1%} "
                      f"(observed {r.observed_ratio:.4f} vs {r.listed_factor:.4f}); tier {t} (approve <= {args.max_error:.0%}, "
                      f"manual_review 3-5%)." + (" NOT applied: individual evidence review required." if t == "manual_review" else ""))})
        counts[t] += 1
    FILE.write_text(json.dumps(doc, indent=1), encoding="utf-8")
    print(counts)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
