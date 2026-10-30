"""AMFI TER: helpers that read the database or the inbox (coverage, report, inbox listing). The pure reader and matcher live in qsde.ingestion.amfi_ter.

Coverage is measured at the LATEST COMPLETE DATE (a last day cut short by a mid-day download is excluded) against the in-scope master schemes: those with a NAV in the 90 days before `today`.
Reports hold counts and scheme names only, never AMFI's TER values (see docs/design/AMFI_TER_DESIGN.md).
"""
from __future__ import annotations

import hashlib
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

from qsde.config.paths import data_path

INBOX = data_path("inbox", "amfi_ter")
IGNORED_SUFFIXES = (".crdownload", ".part", ".tmp")
RECENT_SECONDS = 60
ACTIVE_DAYS = 90
CARRY_DAYS = 7                                    # a TER is a standing rate: a scheme's latest TER up to this many days before the latest complete day still counts
STALE_DAYS = 45
REPORT_NAME_LIMIT = 400


def inbox_files(inbox: Path = INBOX, now: float | None = None) -> tuple[list, list]:
    """(files to load, one message per ignored file). Ignored: Excel lock files, partial downloads, other file types, and anything modified in the last 60 seconds (may still be copying)."""
    now = time.time() if now is None else now
    files, msgs = [], []
    if not Path(inbox).exists():
        return files, [f"inbox {inbox} does not exist"]
    for p in sorted(Path(inbox).iterdir()):
        if not p.is_file():
            continue
        if p.name.startswith("~$"):
            msgs.append(f"ignored {p.name}: Excel lock file (the workbook is open in Excel)")
        elif p.suffix.lower() in IGNORED_SUFFIXES:
            msgs.append(f"ignored {p.name}: partial download")
        elif p.suffix.lower() not in (".xlsx", ".csv"):
            msgs.append(f"ignored {p.name}: not an .xlsx or .csv file")
        elif now - p.stat().st_mtime < RECENT_SECONDS:
            msgs.append(f"ignored {p.name}: modified in the last {RECENT_SECONDS} seconds (may still be copying); run again in a minute")
        else:
            files.append(p)
    return files, msgs


def _files_waiting(cur, inbox: Path) -> int:
    cur.execute("SELECT sha256 FROM mf_ter_files")
    loaded = {r["sha256"].strip() for r in cur.fetchall()}
    return sum(1 for p in inbox_files(inbox)[0] if hashlib.sha256(p.read_bytes()).hexdigest() not in loaded)


def latest_complete_date(cur) -> Optional[date]:
    """The newest TER date that is not the cut-short last day of a file (a later file removes that doubt)."""
    cur.execute("""SELECT max(t.ter_date) AS d FROM mf_ter t WHERE t.ter_date NOT IN (
                       SELECT f.date_max FROM mf_ter_files f WHERE f.last_date_partial AND NOT EXISTS (SELECT 1 FROM mf_ter_files g WHERE g.date_max > f.date_max))""")
    return cur.fetchone()["d"]


def _gaps(cur, today: date, latest: Optional[date]) -> tuple[int, list]:
    """(in-scope count, [(scheme_code, name, reason)] for in-scope schemes whose latest TER on or before `latest` is missing or older than CARRY_DAYS)."""
    cur.execute("""WITH scope AS (SELECT scheme_code, name FROM mf_scheme_master WHERE last_nav_date >= %s::date - %s),
                        lr AS (SELECT m.scheme_code, max(t.ter_date) AS last_date FROM mf_ter_map m
                               JOIN mf_ter t ON t.nsdl_code = m.nsdl_code AND t.ter_date <= %s AND ((m.plan = 'regular' AND t.r_total IS NOT NULL) OR (m.plan = 'direct' AND t.d_total IS NOT NULL))
                               GROUP BY m.scheme_code)
                   SELECT s.scheme_code, s.name, lr.last_date, EXISTS (SELECT 1 FROM mf_ter_map mm WHERE mm.scheme_code = s.scheme_code) AS in_map,
                          (SELECT u.reason FROM mf_ter_unmatched u WHERE u.side = 'master' AND u.scheme_code = s.scheme_code ORDER BY u.file_id DESC LIMIT 1) AS reason
                   FROM scope s LEFT JOIN lr USING (scheme_code)""", (today, ACTIVE_DAYS, latest))
    rows = cur.fetchall()
    gaps = []
    for r in rows:
        if latest is not None and r["last_date"] is not None and (latest - r["last_date"]).days <= CARRY_DAYS:
            continue
        why = (f"stale_over_{CARRY_DAYS}_days" if r["last_date"] is not None else "no_ter_row" if r["in_map"] else (r["reason"] or "not_in_any_loaded_file"))
        gaps.append((r["scheme_code"], r["name"], why))
    return len(rows), gaps


def coverage(conn, today: date, inbox: Path = INBOX) -> dict:
    """Latest complete TER date, in-scope master schemes, how many have a TER that day, the reason for every gap, and inbox files not yet loaded."""
    cur = conn.cursor()
    latest = latest_complete_date(cur)
    in_scope, gaps = _gaps(cur, today, latest)
    reasons: dict = {}
    for _, _, why in gaps:
        reasons[why] = reasons.get(why, 0) + 1
    mapped = in_scope - len(gaps)
    return {"latest_complete_date": latest, "in_scope": in_scope, "mapped": mapped, "pct": round(100 * mapped / in_scope, 1) if in_scope else 0.0,
            "by_reason": reasons, "files_waiting": _files_waiting(cur, Path(inbox))}


def report_info(conn, today: date) -> dict:
    cur = conn.cursor()
    cov = coverage(conn, today)
    _, gaps = _gaps(cur, today, cov["latest_complete_date"])
    cur.execute("""SELECT filename, n_data_rows, n_parsed, n_rejected, n_flagged, n_footer_rows, date_min, date_max, last_date_partial, retrieved_at, left(sha256, 16) AS sha,
                          (issues ->> 'restatements')::int AS restatements, (issues ->> 'overlap_rows')::int AS overlap_rows FROM mf_ter_files ORDER BY id""")
    files = cur.fetchall()
    cur.execute("SELECT f AS flag, count(*) AS n FROM mf_ter, unnest(flags) f GROUP BY f ORDER BY f")
    flags = cur.fetchall()
    cur.execute("""SELECT u.nsdl_code, u.name, u.reason FROM mf_ter_unmatched u WHERE u.side = 'ter' AND u.file_id = (SELECT max(id) FROM mf_ter_files) ORDER BY u.reason, u.name""")
    ter_un = cur.fetchall()
    return {"today": today, "coverage": cov, "files": files, "flag_counts": flags, "gaps": gaps, "ter_unmatched": ter_un}


def write_report(info: dict, out_dir: Path | None = None) -> Path:
    """data/reports/amfi/amfi_ter.md: counts, coverage, and the names of unmatched schemes with reasons. No TER values."""
    cov = info["coverage"]
    L = ["# AMFI expense ratios (TER): ingestion and coverage", "",
         f"> Generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC for {info['today']}. Source: AMFI's TER page, downloaded manually by the owner. Counts and scheme names only; AMFI's values are not reproduced here.", "",
         "## Coverage (roadmap gate 1.1)", "",
         f"- latest complete TER date: {cov['latest_complete_date']}",
         f"- in-scope schemes (NAV in the last {ACTIVE_DAYS} days): {cov['in_scope']}",
         f"- with a TER (latest on or before that date, at most {CARRY_DAYS} days old): {cov['mapped']} ({cov['pct']}%)",
         f"- files waiting in the inbox: {cov['files_waiting']}", "",
         "| gap reason | schemes |", "|---|---|", *[f"| {k} | {v} |" for k, v in sorted(cov["by_reason"].items())], "",
         "## Files", "", "| file | data rows | parsed | rejected | flagged | footer rows | dates | last day partial | restatements | sha256 |", "|---|---|---|---|---|---|---|---|---|---|",
         *[f"| {f['filename']} | {f['n_data_rows']} | {f['n_parsed']} | {f['n_rejected']} | {f['n_flagged']} | {f['n_footer_rows']} | {f['date_min']} to {f['date_max']} | {f['last_date_partial']} | {f['restatements']} | `{f['sha']}` |" for f in info["files"]], "",
         "## Row flags (stored with each row; values are never changed)", "", "| flag | rows |", "|---|---|", *[f"| {f['flag']} | {f['n']} |" for f in info["flag_counts"]], "",
         f"## Unmatched TER schemes (latest file): {len(info['ter_unmatched'])}", "",
         *[f"- {r['name']} ({r['nsdl_code']}): {r['reason']}" for r in info["ter_unmatched"][:REPORT_NAME_LIMIT]], "",
         f"## In-scope schemes without a TER: {len(info['gaps'])}", "",
         *[f"- {name} ({code}): {why}" for code, name, why in sorted(info["gaps"], key=lambda g: (g[2], str(g[1])))[:REPORT_NAME_LIMIT]]]
    out = Path(out_dir) if out_dir else data_path("reports", "amfi")
    out.mkdir(parents=True, exist_ok=True)
    path = out / "amfi_ter.md"
    path.write_text("\n".join(L) + "\n", encoding="utf-8")
    return path
