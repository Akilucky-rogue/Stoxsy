"""Load the local NSE bhavcopy archive into eod_raw and (optionally) rebuild ohlcv.

Usage (from backend/):
    python scripts/load_bhavcopy.py --start 2026-04-01 --end 2026-04-28 --dry-run
    python scripts/load_bhavcopy.py --start 2026-04-01 --end 2026-04-28           # load slice
    python scripts/load_bhavcopy.py --start 2016-01-01 --end 2026-04-30 --rebuild-ohlcv

Writes a Markdown validation report to D:\\Stoxsy\\data\\reports\\.
Nothing is fetched from the network.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from qsde.config.paths import data_path

log = logging.getLogger("load_bhavcopy")
REPORT_DIR = data_path("reports")


def _d(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", type=_d, required=True)
    ap.add_argument("--end", type=_d, required=True)
    ap.add_argument("--base", type=Path, default=None, help="bhavcopy 'nse' folder override")
    ap.add_argument("--dry-run", action="store_true", help="validate only, write nothing")
    ap.add_argument("--rebuild-ohlcv", action="store_true",
                    help="after loading: detect corporate actions and rebuild ohlcv")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    from qsde.ingestion import nse_eod

    kw = {"base": args.base} if args.base else {}
    rep = nse_eod.load_range(args.start, args.end, dry_run=args.dry_run, **kw)
    out = {"range": f"{args.start}..{args.end}", "dry_run": args.dry_run,
           "files": rep.files, "rows_in": rep.rows_in, "rows_valid": rep.rows_valid,
           "rows_rejected": rep.rows_rejected, "rows_loaded": rep.rows_loaded,
           "rejects_by_reason": rep.rejects_by_reason,
           "deliv_per_nulled": rep.deliv_per_nulled,
           "date_mismatch_rows": rep.date_mismatch_rows,
           "stale_files": rep.stale_files,
           "first_date": str(rep.first_date), "last_date": str(rep.last_date)}

    if args.rebuild_ohlcv and not args.dry_run:
        from qsde.ingestion import corp_actions
        out["rebuild"] = corp_actions.rebuild_from_db()

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    name = f"bhavcopy_load_{args.start}_{args.end}{'_dry' if args.dry_run else ''}.json"
    (REPORT_DIR / name).write_text(json.dumps(out, indent=2, default=str))
    sample = REPORT_DIR / name.replace(".json", "_reject_sample.json")
    sample.write_text(json.dumps(rep.rejects_sample, indent=2, default=str))
    print(json.dumps(out, indent=2, default=str))
    print(f"report: {REPORT_DIR / name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
