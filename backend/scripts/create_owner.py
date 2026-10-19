"""Create the first (owner/admin) account. The password never appears on a command line, in chat or in a log.

Two ways:
    1) hidden prompt / environment:   $env:STOXSY_OWNER_PASSWORD = '...' ; python scripts/create_owner.py --email you@example.com
    2) generated temporary password:  python scripts/create_owner.py --email you@example.com --generate-password-file D:\\Stoxsy\\data\\secrets\\owner_bootstrap.txt
       A random 24-character temporary password is written ONLY to that file (restricted to the current user where the OS allows it).
       Read it once, sign in, choose your own password when asked (the account is created with must-change-password), then delete the file.

Refuses to run if an admin already exists (use the admin API / UI to add users). Then set AUTH_ENFORCED=true and JWT_SECRET (>= 32 random
characters) in .env.
"""
from __future__ import annotations

import argparse
import getpass
import os
import secrets
import string
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _temp_password() -> str:
    alphabet = string.ascii_letters + string.digits + "-_.!"
    while True:
        p = "".join(secrets.choice(alphabet) for _ in range(24))
        if any(c.islower() for c in p) and any(c.isupper() for c in p) and any(c.isdigit() for c in p):
            return p


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--email", required=True)
    ap.add_argument("--name", default="Owner")
    ap.add_argument("--generate-password-file", default=None)
    args = ap.parse_args()
    from qsde.auth import service
    from qsde.db.connection import read_sql

    if int(read_sql("SELECT count(*) AS n FROM users WHERE role = 'admin'")["n"].iloc[0]) > 0:
        print("an admin account already exists; use the admin API to add users")
        return 1
    out = None
    if args.generate_password_file:
        out = Path(args.generate_password_file)
        if out.exists():
            print(f"{out} already exists; remove it first (it may hold an old temporary password)")
            return 1
        pw = _temp_password()
    else:
        pw = os.environ.get("STOXSY_OWNER_PASSWORD") or getpass.getpass("Password (min 12 characters): ")
    try:
        u = service.create_user(args.email, args.name, pw, "admin", must_change=True)
    except service.AuthError as e:
        print(f"not created: {e}")
        return 2
    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(f"Stoxsy owner account: {u['email']}\nTemporary password (change it at first sign-in, then delete this file):\n{pw}\n", encoding="utf-8")
        try:
            os.chmod(out, 0o600)
        except OSError:
            pass
        print(f"created admin {u['email']}; the temporary password is in {out} (not shown here)")
    else:
        print(f"created admin {u['email']} (must change password at first sign-in)")
    print("next: AUTH_ENFORCED=true and JWT_SECRET=<32+ random characters> in .env, then restart the API")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
