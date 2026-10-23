"""Ingest a BSE Corporate Actions export (downloaded by the owner from BSE's Corporate Actions page) with the evidence-first pipeline. Applies NOTHING.

    python scripts/load_bse_corporate_actions.py --inbox            # every new file in D:\\Stoxsy\\data\\inbox\\bse_corporate_actions
    python scripts/load_bse_corporate_actions.py --file export.csv  # one file
    python scripts/load_bse_corporate_actions.py --reconcile-only   # re-run the reconciliation over what is already loaded
Steps per file: land the raw bytes (SHA-256, retrieval time, known_at) -> parse and validate (bad rows are reported, never dropped silently) -> insert actions (idempotent on a content hash)
-> reconcile against BSE raw prices -> confirmed | needs_review | rejected. Report: data/reports/bse/bse_corporate_actions.md.
"""
from __future__ import annotations

import argparse
import json
import sys
import warnings
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
warnings.filterwarnings("ignore")

import pandas as pd
import psycopg2.extras

INBOX = Path(r"D:\Stoxsy\data\inbox\bse_corporate_actions")
REPORT = Path(r"D:\Stoxsy\data\reports\bse")
CHUNK = 400


def load_file(path: Path, dry_run: bool, mapping_ok: bool = False) -> dict:
    from qsde.db.connection import get_sync_conn, read_sql
    from qsde.ingestion import bse_corporate_actions as B
    from qsde.stream.base import RawItem
    from qsde.stream.landing import LandingZone

    payload = path.read_bytes()
    ex = B.read_export(payload, path.name)
    ins = read_sql("SELECT id FROM bse_ca_files WHERE sha256 = :s", params={"s": ex.sha256})
    if len(ins):
        return {"file": path.name, "status": "already loaded", "file_id": int(ins["id"].iloc[0])}
    isin = read_sql("SELECT scrip_code, isin FROM bse_listing_master WHERE isin IS NOT NULL")
    frame = B.enrich(ex.frame, dict(zip(isin["scrip_code"].astype(int), isin["isin"])))
    info = {"file": path.name, "rows": ex.n_rows, "parsed": len(frame), "bad": ex.n_bad, "column_mapping": ex.mapping, "unmatched_headers": ex.unmatched,
            "by_type": frame["action_type"].value_counts().to_dict(), "sample": frame[["scrip_code", "ex_date", "purpose", "action_type", "factor"]].head(5).astype(str).to_dict("records"), "issues": ex.issues[:10]}
    first = int(read_sql("SELECT count(*) AS n FROM bse_ca_files")["n"].iloc[0]) == 0
    if dry_run:
        return {"status": "dry run", **info}
    if first and not mapping_ok:                                  # the first real export must be checked by a person: the column mapping was written without seeing BSE's actual file
        return {"status": "REFUSED: first real file needs a reviewed mapping. Run with --dry-run, check column_mapping / sample against the file, then repeat with --mapping-ok", **info}
    now = datetime.now(timezone.utc)
    rec = LandingZone().write("bse_corporate_actions", RawItem(payload, now, None, {"origin": "manual_download", "filename": path.name,
                                                                                 "page": "BSE Corporate Actions page (Download); BSE's API rejects automated clients"}, path.suffix.lstrip(".") or "csv"))
    J = psycopg2.extras.Json
    with get_sync_conn() as conn:
        cur = conn.cursor()
        cur.execute("INSERT INTO bse_ca_files (sha256, filename, origin, n_rows, n_parsed, n_bad, issues, retrieved_at, known_at, landing_path) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                    (ex.sha256, path.name, "manual_download", ex.n_rows, len(frame), ex.n_bad, J(ex.issues), now, now, str(rec.path)))
        fid = cur.fetchone()["id"]
        rows = []
        for r in frame.to_dict("records"):
            d = {k: (None if pd.isna(v) else v) for k, v in r.items() if k != "raw_row"}
            rows.append((fid, int(d["scrip_code"]), d.get("name"), d["purpose"], d["action_type"], d.get("ratio_text"), d.get("factor"), d.get("amount"), d["ex_date"].date(),
                         *(None if d.get(c) is None else pd.Timestamp(d[c]).date() for c in ("record_date", "bc_start", "bc_end", "nd_start", "nd_end", "payment_date")),
                         d.get("isin"), now, B.content_hash(d), J(json.loads(json.dumps(r["raw_row"], default=str)))))
        psycopg2.extras.execute_values(cur, """INSERT INTO bse_corporate_actions (file_id, scrip_code, name, purpose, action_type, ratio_text, factor, amount, ex_date, record_date, bc_start, bc_end,
                                               nd_start, nd_end, payment_date, isin, known_at, content_hash, raw_row) VALUES %s ON CONFLICT (content_hash) DO NOTHING""", rows, page_size=500)
    return {"file": path.name, "status": "loaded", "file_id": fid, "rows": ex.n_rows, "parsed": len(frame), "bad": ex.n_bad, "landed": str(rec.path)}


def reconcile_all(only_unreconciled: bool = True) -> dict:
    from qsde.db.connection import get_sync_conn, read_sql
    from qsde.ingestion import bse_corporate_actions as B

    where = "WHERE NOT EXISTS (SELECT 1 FROM bse_ca_reconciliation r WHERE r.action_id = a.id AND r.version = :v)" if only_unreconciled else ""
    acts = read_sql(f"SELECT a.id, a.scrip_code, a.ex_date, a.purpose, a.action_type, a.factor, a.amount FROM bse_corporate_actions a {where}", params={"v": B.VERSION} if only_unreconciled else {})
    if acts.empty:
        return {"reconciled": 0}
    acts["ex_date"] = pd.to_datetime(acts["ex_date"])
    cal = pd.to_datetime(read_sql("SELECT DISTINCT date FROM bse_eod_raw WHERE date >= CAST(:d AS DATE)", params={"d": str((acts["ex_date"].min() - pd.Timedelta(days=120)).date())})["date"])
    res = []
    codes = sorted(acts["scrip_code"].unique())
    for i in range(0, len(codes), CHUNK):
        part = acts[acts["scrip_code"].isin(codes[i:i + CHUNK])]
        bars = read_sql("SELECT scrip_code, date, open, close FROM bse_eod_raw WHERE scrip_code = ANY(:c) AND date BETWEEN CAST(:a AS DATE) AND CAST(:b AS DATE)",
                        params={"c": [int(c) for c in part["scrip_code"].unique()], "a": str((part["ex_date"].min() - pd.Timedelta(days=120)).date()), "b": str((part["ex_date"].max() + pd.Timedelta(days=30)).date())})
        bars["date"] = pd.to_datetime(bars["date"])
        res.append(B.reconcile(part, bars, calendar=cal))
    r = pd.concat(res, ignore_index=True)
    J = psycopg2.extras.Json
    with get_sync_conn() as conn:
        cur = conn.cursor()
        psycopg2.extras.execute_values(cur, """INSERT INTO bse_ca_reconciliation (action_id, version, classification, reason, listed_factor, observed_ratio, factor_error, gap_date, shift_sessions,
                                               untraded_sessions, persistence_ratio) VALUES %s ON CONFLICT (action_id) DO UPDATE SET version = EXCLUDED.version, classification = EXCLUDED.classification,
                                               reason = EXCLUDED.reason, listed_factor = EXCLUDED.listed_factor, observed_ratio = EXCLUDED.observed_ratio, factor_error = EXCLUDED.factor_error,
                                               gap_date = EXCLUDED.gap_date, shift_sessions = EXCLUDED.shift_sessions, untraded_sessions = EXCLUDED.untraded_sessions,
                                               persistence_ratio = EXCLUDED.persistence_ratio, reconciled_at = NOW()""",
                                       [(int(x.id), B.VERSION, x.classification, str(x.reason)[:500], None if pd.isna(x.listed_factor) else float(x.listed_factor),
                                         None if pd.isna(x.observed_ratio) else float(x.observed_ratio), None if pd.isna(x.factor_error) else float(x.factor_error),
                                         None if x.gap_date is None or pd.isna(x.gap_date) else pd.Timestamp(x.gap_date).date(), None if pd.isna(x.shift_sessions) else int(x.shift_sessions),
                                         None if pd.isna(x.untraded_sessions) else int(x.untraded_sessions), None if pd.isna(x.persistence_ratio) else float(x.persistence_ratio)) for x in r.itertuples()])
    return {"reconciled": int(len(r)), **r["classification"].value_counts().to_dict()}


def reclassify_all() -> dict:
    """Recompute the DERIVED columns (action_type, ratio_text, factor, amount) from the stored purpose text with the current classifier. The raw purpose and the content hash are untouched."""
    from qsde.db.connection import get_sync_conn, read_sql
    from qsde.ingestion import bse_corporate_actions as B

    a = read_sql("SELECT id, purpose, action_type FROM bse_corporate_actions")
    info = a["purpose"].map(B.classify_purpose)
    rows = [(i["action_type"], i["ratio_text"], i["factor"], i["amount"], int(x)) for i, x in zip(info, a["id"])]
    changed = int(sum(1 for i, old in zip(info, a["action_type"]) if i["action_type"] != old))
    with get_sync_conn() as conn:
        psycopg2.extras.execute_batch(conn.cursor(), "UPDATE bse_corporate_actions SET action_type = %s, ratio_text = %s, factor = %s, amount = %s WHERE id = %s", rows, page_size=1000)
    return {"reclassified": len(rows), "action_type_changed": changed}


def report() -> str:
    from qsde.db.connection import read_sql

    t = read_sql("""SELECT a.action_type, coalesce(r.classification, 'not reconciled') AS classification, count(*) AS n FROM bse_corporate_actions a
                    LEFT JOIN bse_ca_reconciliation r ON r.action_id = a.id GROUP BY 1, 2 ORDER BY 1, 2""")
    f = read_sql("SELECT filename, n_rows, n_parsed, n_bad, retrieved_at, sha256 FROM bse_ca_files ORDER BY id")
    L = ["# BSE corporate actions: ingestion and reconciliation", "",
         "> Evidence only. Nothing here adjusts a price. BSE stays `raw_prices_only`; BSE-only names stay out of the universe, the defensive signal and decision cards until a BSE adjusted-price materialisation "
         "is built from `confirmed` actions AND validated AND approved by the owner.", "",
         f"- files loaded: {len(f)}; actions: {int(t['n'].sum()) if len(t) else 0}", "", "| action type | classification | n |", "|---|---|---|", *[f"| {r.action_type} | {r.classification} | {int(r.n)} |" for r in t.itertuples()], "",
         "## Files", "", *[f"- `{r.filename}`: {r.n_rows} rows, {r.n_parsed} parsed, {r.n_bad} rejected; retrieved {pd.Timestamp(r.retrieved_at):%Y-%m-%d %H:%M} UTC; sha256 `{r.sha256[:16]}`" for r in f.itertuples()]]
    REPORT.mkdir(parents=True, exist_ok=True)
    (REPORT / "bse_corporate_actions.md").write_text("\n".join(L), encoding="utf-8")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--file")
    ap.add_argument("--inbox", action="store_true")
    ap.add_argument("--reconcile-only", action="store_true")
    ap.add_argument("--reclassify", action="store_true", help="re-derive action types / factors from the stored purpose text with the current classifier, then reconcile everything again")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--mapping-ok", action="store_true", help="I have checked the column mapping and sample of a dry run against the real file")
    a = ap.parse_args()
    files = [Path(a.file)] if a.file else (sorted(p for p in INBOX.glob("*") if p.suffix.lower() in (".csv", ".xlsx", ".xls")) if a.inbox else [])
    if a.inbox and not files:
        print(f"nothing in {INBOX}: download the export from BSE's Corporate Actions page and save it there")
    for p in files:
        try:
            print(json.dumps(load_file(p, a.dry_run, a.mapping_ok), default=str))
        except ValueError as e:
            print(f"REJECTED {p.name}: {e}")
            return 2
    if a.dry_run:
        return 0
    if a.reclassify and not a.dry_run:
        print(json.dumps(reclassify_all()))
        a.reconcile_only = True
    if files or a.reconcile_only:
        print(json.dumps(reconcile_all(only_unreconciled=not a.reconcile_only), default=str))
        print(report())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
