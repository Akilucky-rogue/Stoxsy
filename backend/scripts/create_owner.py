"""Create the first (owner/admin) account. The password is read from the environment or a hidden prompt - never from the command line,
never echoed, never logged.

    set STOXSY_OWNER_PASSWORD=...        (PowerShell: $env:STOXSY_OWNER_PASSWORD = '...')
    python scripts/create_owner.py --email you@example.com --name "Your Name"

Refuses to run if an admin already exists (use the admin API / UI to add users). The account is created with must-change-password set,
so the first sign-in forces a new password. Then set AUTH_ENFORCED=true and JWT_SECRET (>= 32 random characters) in .env.
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--email", required=True)
    ap.add_argument("--name", default="Owner")
    args = ap.parse_args()
    from qsde.auth import service
    from qsde.db.connection import read_sql

    if int(read_sql("SELECT count(*) AS n FROM users WHERE role = 'admin'")["n"].iloc[0]) > 0:
        print("an admin account already exists; use the admin API to add users")
        return 1
    pw = os.environ.get("STOXSY_OWNER_PASSWORD") or getpass.getpass("Password (min 12 characters): ")
    try:
        u = service.create_user(args.email, args.name, pw, "admin", must_change=True)
    except service.AuthError as e:
        print(f"not created: {e}")
        return 2
    print(f"created admin {u['email']} (must change password at first sign-in)")
    print("next: set AUTH_ENFORCED=true and JWT_SECRET=<32+ random characters> in .env, then restart the API")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
