"""
Apply every SQL migration in infra/migrations/, in order, idempotently.

There is no migration-tracking table — migrations are written to be re-runnable
(CREATE TABLE IF NOT EXISTS / ADD COLUMN IF NOT EXISTS / DO $$ guards), so this
just runs them all in filename order against the configured DB. Each file is its
own transaction: a file that errors is rolled back and reported, the rest still
apply. Run this after pulling new migrations so the schema can never silently
drift (the bug where 013 got skipped and broke the Tier-1 pipeline).

Usage (from backend, venv active):
    python scripts/apply_migrations.py
    python scripts/apply_migrations.py --list      # show files, apply nothing
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qsde.db.connection import get_sync_conn

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("migrate")

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "infra" / "migrations"


def main() -> None:
    ap = argparse.ArgumentParser(description="Apply infra/migrations/*.sql idempotently")
    ap.add_argument("--list", action="store_true", help="List migrations and exit.")
    args = ap.parse_args()

    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if not files:
        log.error("No migrations found in %s", MIGRATIONS_DIR)
        sys.exit(1)
    if args.list:
        for f in files:
            print(f.name)
        return

    ok, failed = 0, []
    for f in files:
        sql = f.read_text(encoding="utf-8")
        try:
            with get_sync_conn() as conn:        # commits on success, rolls back on error
                conn.cursor().execute(sql)
            ok += 1
            log.info("applied %s", f.name)
        except Exception as e:  # noqa: BLE001 — one bad/non-idempotent file must not block the rest
            failed.append((f.name, str(e).splitlines()[0]))
            log.warning("FAILED %s: %s", f.name, str(e).splitlines()[0])

    log.info("=" * 60)
    log.info("migrations: %d applied OK, %d failed", ok, len(failed))
    for name, err in failed:
        log.warning("  %s -> %s", name, err)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
