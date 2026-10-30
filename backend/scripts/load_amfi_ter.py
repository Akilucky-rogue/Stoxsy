"""Load AMFI "TER of MF Schemes" downloads (saved by the owner from AMFI's TER page) raw-first and idempotently. Applies NOTHING to prices or models.

    python scripts/load_amfi_ter.py --inbox --dry-run     # read every new file in <data>/inbox/amfi_ter, write nothing, show mapping and counts
    python scripts/load_amfi_ter.py --inbox               # load (the first file of each layout needs --mapping-ok after a dry run)
    python scripts/load_amfi_ter.py --file ter.xlsx
Steps per file: parse (no side effects) -> skip if its SHA-256 is already loaded -> layout gate -> land the raw bytes -> ONE transaction: layout, file, schemes, daily rows
(first-seen value kept, restatements counted), mapping to mf_scheme_master, unmatched list. See docs/design/AMFI_TER_DESIGN.md.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import warnings
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from qsde.config.paths import data_path
warnings.filterwarnings("ignore")

import pandas as pd
import psycopg2.extras

from qsde.ingestion.amfi_ter_db import INBOX, coverage, inbox_files, report_info, write_report  # noqa: F401  (re-exported for callers and tests)

NUM = ["r_ber", "r_brokerage", "r_transaction", "r_statutory", "r_total", "d_ber", "d_brokerage", "d_transaction", "d_statutory", "d_total"]
RESTATEMENT_TOL = 0.00005                           # AMFI publishes in steps of 0.0001; stored values are exact, so any real change exceeds this


def _none(x):
    return None if x is None or (isinstance(x, float) and math.isnan(x)) else x


def origin_of(filename: str) -> str:
    """How a file reached the inbox, recorded with the landed raw copy: `stoxsy fetch-ter` names its files ter_revised_MM-YYYY__<date>.xlsx; anything else was saved by hand."""
    return "fetch_ter" if filename.startswith("ter_revised_") else "manual_download"


def load_file(path: Path, dry_run: bool, mapping_ok: bool = False, conn=None, landing=None) -> dict:
    """Parse, gate, land and load one file. With `conn` the caller owns the transaction (nothing is committed or closed here); without it one is opened and committed on success.
    A file whose SHA-256 is already in the ledger is recognised BEFORE it is parsed, so the nightly inbox step costs the same however many months have accumulated."""
    from qsde.ingestion import amfi_ter as A

    path = Path(path)
    payload = path.read_bytes()
    sha = hashlib.sha256(payload).hexdigest()
    if conn is not None:
        return _load_new(conn, path, payload, sha, dry_run, mapping_ok, landing, A)
    from qsde.db.connection import get_sync_conn

    with get_sync_conn() as c:
        return _load_new(c, path, payload, sha, dry_run, mapping_ok, landing, A)


def _load_new(conn, path: Path, payload: bytes, sha: str, dry_run: bool, mapping_ok: bool, landing, A) -> dict:
    cur = conn.cursor()
    cur.execute("SELECT id FROM mf_ter_files WHERE sha256 = %s", (sha,))
    row = cur.fetchone()
    if row:
        return {"file": path.name, "status": "already loaded", "file_id": int(row["id"])}
    ex = A.read_export(payload, path.name)                                  # ValueError = refused file, before anything is written
    return _load(conn, path, payload, ex, dry_run, mapping_ok, landing)


def _load(conn, path: Path, payload: bytes, ex, dry_run: bool, mapping_ok: bool, landing) -> dict:
    from qsde.ingestion import amfi_ter as A
    from qsde.stream.base import RawItem
    from qsde.stream.landing import LandingZone

    cur = conn.cursor()
    cur.execute("SELECT id FROM mf_ter_files WHERE sha256 = %s", (ex.sha256,))
    row = cur.fetchone()
    if row:
        return {"file": path.name, "status": "already loaded", "file_id": int(row["id"])}
    frame = ex.frame.assign(flags=A.flag_rows(ex.frame))
    partial = A.partial_dates(ex.frame)
    ter = A.ter_schemes(ex.frame)
    cur.execute("SELECT scheme_code, name, amc, category_raw, is_direct, is_growth, is_etf, last_nav_date FROM mf_scheme_master")
    master = pd.DataFrame(cur.fetchall())
    if master.empty:
        raise ValueError("mf_scheme_master is empty: TER rows cannot be matched to schemes")
    d_min, d_max = frame["ter_date"].min().date(), frame["ter_date"].max().date()
    match = A.match_schemes(ter, master, d_max)
    cur.execute("SELECT approved FROM mf_ter_layouts WHERE layout_hash = %s", (ex.layout_hash,))
    lr = cur.fetchone()
    approved = bool(lr and lr["approved"])
    n_flagged = int((frame["flags"].map(len) > 0).sum())
    info = {"file": path.name, "sha256": ex.sha256, "sheet": ex.sheet, "data_rows": ex.n_data_rows, "parsed": ex.n_parsed, "rejected": ex.n_rejected, "rejected_head": ex.rejected[:5],
            "footer_rows": ex.n_footer_rows, "footer_head": ex.footer_head[:60], "column_mapping": ex.mapping, "unmatched_headers": ex.unmatched_headers, "layout_hash": ex.layout_hash,
            "layout_approved": approved, "dates": [str(d_min), str(d_max)], "n_dates": int(frame["ter_date"].nunique()), "partial_dates": [str(d) for d in partial],
            "schemes": int(ter.shape[0]), "flagged_rows": n_flagged,
            "flag_counts": {k: int(frame["flags"].map(lambda x, k=k: k in x).sum()) for k in A.FLAGS},
            "ter_codes_mapped": int(match.mapped["nsdl_code"].nunique()),
            "ter_unmatched": match.unmatched.query("side == 'ter'")["reason"].value_counts().to_dict(),
            "master_gaps": match.unmatched.query("side == 'master'")["reason"].value_counts().to_dict(),
            "sample": frame.head(5)[["nsdl_code", "name", "ter_date", "r_total", "d_total"]].astype(str).to_dict("records")}
    if dry_run:
        return {"status": "dry run", **info}
    if not approved and not mapping_ok:                                       # the first file of a layout must be checked by a person
        return {"status": "REFUSED: first file of this layout needs a reviewed mapping. Run with --dry-run, check column_mapping and sample against the file, then repeat with --mapping-ok", **info}

    cur.execute("SELECT max(date_max) AS d FROM mf_ter_files")
    newest_before = cur.fetchone()["d"]
    newest = newest_before is None or d_max >= newest_before                 # an older month loaded later must not re-derive (and overwrite) the mapping from older names
    now = datetime.now(timezone.utc)
    landing = landing or LandingZone()
    rec = landing.write("amfi_ter", RawItem(payload, now, None, {"origin": origin_of(path.name), "filename": path.name, "page": "AMFI TER of MF Schemes (Download Excel)"}, path.suffix.lstrip(".") or "xlsx"))
    J = psycopg2.extras.Json
    cur.execute("""INSERT INTO mf_ter_layouts (layout_hash, headers, approved, approved_at) VALUES (%s, %s, %s, CASE WHEN %s THEN NOW() END)
                   ON CONFLICT (layout_hash) DO UPDATE SET approved = mf_ter_layouts.approved OR EXCLUDED.approved, approved_at = COALESCE(mf_ter_layouts.approved_at, EXCLUDED.approved_at)""",
                (ex.layout_hash, J(list(ex.mapping) + ex.unmatched_headers), True, True))
    # restatements: a (code, date) already stored with a different value. Counted before inserting; the stored (first-seen) value stays.
    cur.execute(f"SELECT nsdl_code, ter_date, {', '.join(NUM)} FROM mf_ter WHERE ter_date BETWEEN %s AND %s", (d_min, d_max))
    old = pd.DataFrame(cur.fetchall())
    overlap = restated = 0
    if not old.empty:
        old["ter_date"] = pd.to_datetime(old["ter_date"])
        for c in NUM:
            old[c] = pd.to_numeric(old[c])
        j = frame.merge(old, on=["nsdl_code", "ter_date"], suffixes=("", "_old"))
        overlap = len(j)
        diff = pd.Series(False, index=j.index)
        for c in NUM:
            a, b = j[c], j[c + "_old"]
            diff |= (a.isna() != b.isna()) | ((a - b).abs() > RESTATEMENT_TOL)
        restated = int(diff.sum())
    issues = {"rejected_head": ex.rejected[:10], "restatements": restated, "overlap_rows": overlap, "footer_head": ex.footer_head[:200]}
    cur.execute("""INSERT INTO mf_ter_files (sha256, filename, layout_hash, n_data_rows, n_parsed, n_rejected, n_flagged, n_footer_rows, footer_head, date_min, date_max, last_date_partial,
                                             issues, retrieved_at, known_at, landing_path) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                (ex.sha256, path.name, ex.layout_hash, ex.n_data_rows, ex.n_parsed, ex.n_rejected, n_flagged, ex.n_footer_rows, ex.footer_head[:200], d_min, d_max,
                 bool(partial and partial[-1] == d_max), J(issues), now, now, str(rec.path)))
    file_id = int(cur.fetchone()["id"])
    s = frame.sort_values("ter_date").groupby("nsdl_code").agg(raw_code=("nsdl_code_raw", "last"), scheme_name=("name", "last"), stype=("scheme_type", "last"), category=("scheme_category", "last"),
                                                              first_d=("ter_date", "min"), last_d=("ter_date", "max"), variants=("name", lambda x: sorted(set(x))))
    psycopg2.extras.execute_values(cur, """INSERT INTO mf_ter_scheme (nsdl_code, nsdl_code_raw, name, scheme_type, scheme_category, first_seen, last_seen, name_variants) VALUES %s
        ON CONFLICT (nsdl_code) DO UPDATE SET first_seen = LEAST(mf_ter_scheme.first_seen, EXCLUDED.first_seen), last_seen = GREATEST(mf_ter_scheme.last_seen, EXCLUDED.last_seen),
            nsdl_code_raw = CASE WHEN EXCLUDED.last_seen >= mf_ter_scheme.last_seen THEN EXCLUDED.nsdl_code_raw ELSE mf_ter_scheme.nsdl_code_raw END,
            name = CASE WHEN EXCLUDED.last_seen >= mf_ter_scheme.last_seen THEN EXCLUDED.name ELSE mf_ter_scheme.name END,
            scheme_type = COALESCE(EXCLUDED.scheme_type, mf_ter_scheme.scheme_type), scheme_category = COALESCE(EXCLUDED.scheme_category, mf_ter_scheme.scheme_category),
            name_variants = (SELECT COALESCE(jsonb_agg(DISTINCT v), '[]'::jsonb) FROM jsonb_array_elements(mf_ter_scheme.name_variants || EXCLUDED.name_variants) v)""",
                                   [(code, r.raw_code, r.scheme_name, _none(r.stype), _none(r.category), r.first_d.date(), r.last_d.date(), J(r.variants)) for code, r in s.iterrows()], page_size=1000)
    dates = frame["ter_date"].dt.date.tolist()
    cols = [frame[c].to_numpy() for c in NUM]
    recs = [(c, d, *(None if math.isnan(a[i]) else float(a[i]) for a in cols), f, file_id, now)
            for i, (c, d, f) in enumerate(zip(frame["nsdl_code"].tolist(), dates, frame["flags"].tolist()))]
    psycopg2.extras.execute_values(cur, f"INSERT INTO mf_ter (nsdl_code, ter_date, {', '.join(NUM)}, flags, file_id, known_at) VALUES %s ON CONFLICT (nsdl_code, ter_date) DO NOTHING", recs, page_size=5000)
    cur.execute("SELECT count(*) AS n FROM mf_ter WHERE file_id = %s", (file_id,))
    rows_new = int(cur.fetchone()["n"])
    pairs = _write_mapping(cur, ter["nsdl_code"].tolist(), match, file_id) if newest else []
    return {"file": path.name, "status": "loaded", "mapping_kept": not newest, "file_id": file_id, "parsed": ex.n_parsed, "rejected": ex.n_rejected, "rows_new": rows_new, "overlap_rows": overlap, "restatements": restated,
            "flagged_rows": n_flagged, "mapped_rows": len(pairs), "landed": str(rec.path), "partial_dates": [str(d) for d in partial]}


def _write_mapping(cur, codes: list, match, file_id: int) -> list:
    """Replace the derived link tables for these TER codes: mf_ter_map (one code per scheme and plan) and the unmatched list of this file."""
    from qsde.ingestion import amfi_ter as A

    cur.execute("DELETE FROM mf_ter_map WHERE nsdl_code = ANY(%s)", (codes,))
    pairs = [(int(r.scheme_code), r.plan) for r in match.mapped.itertuples()]
    if pairs:
        psycopg2.extras.execute_values(cur, "DELETE FROM mf_ter_map m USING (VALUES %s) v(scheme_code, plan) WHERE m.scheme_code = v.scheme_code AND m.plan = v.plan", pairs)
        psycopg2.extras.execute_values(cur, "INSERT INTO mf_ter_map (nsdl_code, scheme_code, plan, key_version) VALUES %s",
                                       [(r.nsdl_code, int(r.scheme_code), r.plan, A.KEY_VERSION) for r in match.mapped.itertuples()], page_size=2000)
    cur.execute("DELETE FROM mf_ter_unmatched WHERE file_id = %s", (file_id,))
    un = match.unmatched
    if len(un):
        psycopg2.extras.execute_values(cur, "INSERT INTO mf_ter_unmatched (file_id, side, nsdl_code, scheme_code, name, reason) VALUES %s",
                                       [(file_id, r.side, _none(r.nsdl_code), None if pd.isna(r.scheme_code) else int(r.scheme_code), _none(r.name), r.reason) for r in un.itertuples()], page_size=2000)
    return pairs


def rematch(conn) -> dict:
    """Rebuild the derived links (mf_ter_map and the latest file's unmatched list) from what is already stored, without re-reading a file. Use after the scheme master or the key rule changes."""
    from qsde.ingestion import amfi_ter as A

    cur = conn.cursor()
    cur.execute("SELECT max(id) AS id, max(date_max) AS d FROM mf_ter_files")
    row = cur.fetchone()
    if row["id"] is None:
        raise ValueError("no TER file is loaded: nothing to match")
    cur.execute("""SELECT s.nsdl_code, s.name, s.scheme_category, bool_or(t.r_total IS NOT NULL) AS r_offered, bool_or(t.d_total IS NOT NULL) AS d_offered
                   FROM mf_ter_scheme s JOIN mf_ter t USING (nsdl_code) GROUP BY s.nsdl_code, s.name, s.scheme_category""")
    ter = pd.DataFrame(cur.fetchall())
    cur.execute("SELECT scheme_code, name, amc, category_raw, is_direct, is_growth, is_etf, last_nav_date FROM mf_scheme_master")
    master = pd.DataFrame(cur.fetchall())
    match = A.match_schemes(ter, master, row["d"])
    pairs = _write_mapping(cur, ter["nsdl_code"].tolist(), match, int(row["id"]))
    return {"codes": len(ter), "mapped_rows": len(pairs), "unmatched_rows": len(match.unmatched), "ter_unmatched": match.unmatched.query("side == 'ter'")["reason"].value_counts().to_dict(),
            "master_gaps": match.unmatched.query("side == 'master'")["reason"].value_counts().to_dict()}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--file")
    ap.add_argument("--inbox", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--rematch", action="store_true", help="rebuild the links to mf_scheme_master from the stored rows (after the master or the key rule changes), then write the report")
    ap.add_argument("--report", action="store_true", help="write data/reports/amfi/amfi_ter.md from what is loaded (also done after every load)")
    ap.add_argument("--mapping-ok", action="store_true", help="I have checked the column mapping and sample of a dry run against the real file")
    a = ap.parse_args()
    if a.file:
        files, msgs = [Path(a.file)], []
    elif a.inbox:
        files, msgs = inbox_files(INBOX)
    elif a.report or a.rematch:
        files, msgs = [], []
    else:
        ap.error("give --file, --inbox, --report or --rematch")
    for m in msgs:
        print(m)
    if a.inbox and not files and not a.report and not a.rematch:
        print(f"nothing to load in {INBOX}: download the month from AMFI's TER page (Download Excel) and save it there")
        return 0
    loaded, rc = False, 0
    if a.rematch and not a.dry_run:
        from qsde.db.connection import get_sync_conn

        with get_sync_conn() as c:
            print(json.dumps(rematch(c), default=str))
    for p in files:
        try:
            r = load_file(p, a.dry_run, a.mapping_ok)
            print(json.dumps(r, default=str))
            loaded = loaded or r.get("status") == "loaded"
        except ValueError as e:                                              # a refused file: report it, keep going with the others, exit 2 at the end
            print(f"REJECTED {p.name}: {e}")
            rc = 2
    if (loaded or a.report or a.rematch) and not a.dry_run:
        from datetime import date

        from qsde.db.connection import get_sync_conn

        with get_sync_conn() as c:
            print("report:", write_report(report_info(c, date.today())))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
