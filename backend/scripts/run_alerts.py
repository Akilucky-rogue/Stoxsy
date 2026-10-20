"""Evaluate the alert rules now (system-wide, plus the portfolio rules of every user who has imported holdings).

    python scripts/run_alerts.py
Safe to run from a scheduler every few minutes: idempotent, read-only apart from the alert tables, nothing is sent anywhere.
"""
from __future__ import annotations

import json
import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
warnings.filterwarnings("ignore")


def main() -> int:
    from qsde.alerts import engine
    from qsde.db.connection import read_sql

    users = read_sql("SELECT DISTINCT user_id FROM portfolio_imports")["user_id"].tolist()
    out = {"global": engine.evaluate(include_global=True)["global"]}
    for u in users:
        out[f"user:{u}"] = engine.evaluate(user_id=u, include_global=False)["user"]
    print(json.dumps(out, default=str, indent=1))
    return 0 if not any(v["errors"] for v in out.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
