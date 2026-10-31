"""Add the newest sessions to the Parquet feature store as an increment snapshot (see qsde/features/refresh.py). Nightly step `features_refresh`; safe to run by hand.

    python scripts/refresh_features.py --dry-run          # what would be added, writes nothing
    python scripts/refresh_features.py                    # both versions (ffd_v2s, ffd_v2)
    python scripts/refresh_features.py --version ffd_v2s

Skips (exit 0) when the store is already current. Exits 1 if any version fails; the other versions are still attempted."""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="append", choices=["ffd_v2s", "ffd_v2"], help="repeatable; default both")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    from qsde.features import refresh as R

    failed = 0
    for v in args.version or list(R.VERSIONS):
        try:
            print(json.dumps(R.refresh(v, dry_run=args.dry_run), default=str), flush=True)
        except Exception as e:                                  # noqa: BLE001
            failed += 1
            print(json.dumps({"version": v, "status": "FAILED", "error": f"{type(e).__name__}: {str(e)[:300]}"}), flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
