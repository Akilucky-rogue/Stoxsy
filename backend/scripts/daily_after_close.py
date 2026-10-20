"""One command for the daily routine after the market close (run after about 19:00 IST, before the next session opens at 09:15 IST).

    python scripts/daily_after_close.py              # everything, in order
    python scripts/daily_after_close.py --dry-run    # print the plan only
    python scripts/daily_after_close.py --only log_cards,alerts
Steps run in order; a REQUIRED step that fails stops the run (nothing downstream may use stale inputs), an optional one is reported and the run continues. The prediction-logging step refuses
to log after the next session's open (the ledger enforces it). Nothing here trades, promotes a model or changes an adjustment decision; network use is limited to the approved public archives
(NSE, BSE, AMFI). Log: data/reports/daily/daily_<date>.log.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

PY = sys.executable
HERE = Path(__file__).resolve().parent
LOG_DIR = Path(r"D:\Stoxsy\data\reports\daily")
STREAM = [str(HERE / "stream_run.py"), "--mode", "once", "--enable-network", "--sink", "db"]

# name, required, description, argv
STEPS = [
    ("nse_cash", True, "NSE cash end-of-day prices", [PY, *STREAM, "--source", "bhavcopy"]),
    ("nse_corp_actions", False, "NSE corporate-action records", [PY, *STREAM, "--source", "corporate_actions"]),
    ("rebuild_ohlcv", True, "rebuild the official `ohlcv` series from raw prices and the applied events (no decision changes)",
     [PY, "-c", "import sys; sys.path.insert(0, r'" + str(HERE.parent) + "'); from qsde.ingestion import corp_actions as c; print(c.rebuild_from_db())"]),
    ("nse_fo", False, "NSE F&O end-of-day archive", [PY, *STREAM, "--source", "nse_fo_bhavcopy"]),
    ("bse_cash", False, "BSE end-of-day prices (raw only)", [PY, *STREAM, "--source", "bse_bhavcopy"]),
    ("amfi_nav", False, "AMFI NAVs (today's file)", [PY, *STREAM, "--source", "amfi_navall"]),
    ("signal", True, "defensive signal for the latest session", [PY, str(HERE / "defensive_eval.py"), "--latest"]),
    ("log_cards", False, "log the signal in the prediction ledger (before the next open)", [PY, str(HERE / "log_cards.py")]),
    ("resolve", False, "resolve logged predictions whose window has elapsed", [PY, str(HERE / "live_loop.py"), "resolve"]),
    ("fund_snapshot", False, "fund returns / risk / quality snapshot", [PY, str(HERE / "fund_snapshot.py")]),
    ("alerts", False, "evaluate alerts", [PY, str(HERE / "run_alerts.py")]),
]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only", default="", help="comma-separated step names")
    a = ap.parse_args()
    only = {s for s in a.only.split(",") if s}
    todo = [s for s in STEPS if not only or s[0] in only]
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log = LOG_DIR / f"daily_{datetime.now():%Y%m%d}.log"
    results = []
    print(f"daily routine, {datetime.now():%Y-%m-%d %H:%M}; {len(todo)} steps; log {log}")
    for name, required, desc, argv in todo:
        print(f"- {name}{' (required)' if required else ''}: {desc}")
        if a.dry_run:
            continue
        t0 = time.time()
        with log.open("a", encoding="utf-8") as fh:
            fh.write(f"\n===== {datetime.now():%H:%M:%S} {name}: {' '.join(argv)}\n")
            r = subprocess.run(argv, stdout=fh, stderr=subprocess.STDOUT, cwd=str(HERE.parent))
        ok = r.returncode == 0
        results.append((name, ok, time.time() - t0))
        print(f"    {'ok' if ok else 'FAILED (exit ' + str(r.returncode) + ')'} in {time.time() - t0:.0f}s")
        if not ok and required:
            print(f"    STOPPED: {name} is required and failed; see {log}")
            break
    if not a.dry_run:
        bad = [n for n, ok, _ in results if not ok]
        print("summary: " + ("all steps ok" if not bad else "failed: " + ", ".join(bad)))
        return 1 if bad else 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
