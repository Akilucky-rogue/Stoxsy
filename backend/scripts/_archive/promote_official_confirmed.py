"""Move the owner-named held items into the `official_confirmed` tier, preserving the audit trail.

Owner decision 2026-10-18: approve the eight 3-5% rename proposals and UEL, confirm DRCSYSTEMS; keep NDRAUTO and VERTOZ held.
This script only EDITS THE APPROVALS FILE (versioned in git); whether an entry is actually applied is decided at rebuild time by
`corp_actions_approvals.verify_official` + the hard bounds, so an entry that no longer qualifies is refused, not forced.

For each entry it appends to `history` (the previous state, untouched), then sets tier / decision / evidence / evidence_pack.
    python scripts/promote_official_confirmed.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from qsde.config.paths import data_path

FILE = Path(__file__).resolve().parents[2] / "infra" / "data" / "corp_action_approvals.json"
EVIDENCE = data_path("reports", "corp_actions", "evidence")
WHEN = "2026-10-18"
# (feed symbol, traded symbol, listed ex-date): the exact entries named by the owner, not every entry of the same pair
PROMOTE = {("UEL", None, "2025-10-08"), ("DRCSYSTEMS", None, "2022-03-16"), ("AURIGROW", "GODHA", "2022-03-24"),
           ("CCAVENUE", "INFIBEAM", "2022-03-14"), ("COSMOFIRST", "COSMOFILMS", "2022-06-16"), ("DHARAN", "KBCGLOBAL", "2025-04-04"),
           ("GANESHHOU", "GANESHHOUC", "2016-07-14"), ("LANDSMILL", "EXCEL", "2018-01-23"), ("MOTHERSON", "MOTHERSUMI", "2018-10-30"),
           ("SECURKLOUD", "8KMILES", "2016-10-10")}
HELD = {("NDRAUTO", None, "2023-07-24"), ("VERTOZ", None, "2025-06-24")}


def main() -> int:
    from qsde.db.connection import read_sql
    from qsde.ingestion.corp_actions_approvals import CA_API_URL

    doc = json.loads(FILE.read_text(encoding="utf-8"))
    changed = 0
    for d in doc["decisions"]:
        key = (d["symbol"], d.get("apply_to_symbol"), d["listed_ex_date"])
        if key in HELD:
            d.setdefault("history", [])
            if not any("official-record review" in h.get("note", "") for h in d["history"]):
                d["history"].append({"at": WHEN, "state": {"decision": d["decision"]},
                                     "note": "official-record review: the NSE record confirms action type, ratio and ex-date, but the "
                                             "hard timing/factor bounds are NOT met, so the official record does not override them. Stays held."})
            continue
        if key not in PROMOTE or d.get("tier") == "official_confirmed":
            continue
        pack = f"{d['symbol']}_{d['listed_ex_date']}.md"
        if not (EVIDENCE / pack).exists():
            print(f"SKIP {key}: evidence pack {pack} missing")
            continue
        api = read_sql("SELECT subject, factor, ex_date, record_date, face_value, isin, known_at FROM nse_corporate_actions "
                       "WHERE symbol = :s AND category IN ('SPLIT_BONUS', 'CONSOLIDATION') AND ex_date BETWEEN "
                       "CAST(:a AS DATE) - 12 AND CAST(:a AS DATE) + 12", params={"s": d["symbol"], "a": d["listed_ex_date"]})
        api = api[(api["factor"].astype(float) / float(d["factor"]) - 1).abs() < 1e-6]
        if api.empty:
            print(f"SKIP {key}: no matching NSE record")
            continue
        a = api.sort_values("known_at").iloc[0]
        d.setdefault("history", []).append({
            "at": WHEN, "note": "state before reclassification to official_confirmed (original approval trail preserved)",
            "state": {"decision": d["decision"], "confidence": d.get("confidence"), "decided_by": d.get("decided_by"),
                      "decided_at": d.get("decided_at"), "notes": d.get("notes")}})
        d.pop("confidence", None)
        d.update({"decision": "approve", "tier": "official_confirmed", "evidence_pack": pack,
                  "decided_by": "owner (official_confirmed tier)", "decided_at": WHEN,
                  "evidence": {"nse_subject": a["subject"], "nse_factor": float(a["factor"]), "nse_ex_date": str(a["ex_date"]),
                               "nse_record_date": None if a["record_date"] is None else str(a["record_date"]),
                               "face_value": None if a["face_value"] != a["face_value"] else float(a["face_value"]), "isin": a["isin"],
                               "source_url": CA_API_URL, "retrieved_at": str(a["known_at"])},
                  "notes": (d.get("notes") or "") + " | RECLASSIFIED 2026-10-18 to official_confirmed: NSE record matches action, "
                           "ratio and ex-date; ex-date on the gap bar or inside the untraded window; factor inside the hard bound."})
        changed += 1
    FILE.write_text(json.dumps(doc, indent=1), encoding="utf-8")
    print(f"promoted {changed} entries to official_confirmed; NDRAUTO and VERTOZ left held")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
