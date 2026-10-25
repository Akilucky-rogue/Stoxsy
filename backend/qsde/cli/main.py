"""`stoxsy`: the operator console. Non-interactive by default so cron, systemd timers and scripts can call it; the exit code carries the result (0 ok, 1 warnings, 2 failures).

    stoxsy status                       one-screen summary: freshness, heartbeats, Kite, alerts
    stoxsy doctor                       every health check with the reason; exit code for monitors
    stoxsy coverage [--out DIR]         the data-coverage matrix (text, plus Markdown and a visual HTML report)
    stoxsy heartbeat                    liveness of the stream and the daily routine
    stoxsy ingest daily|deals|fno|nse|bse|amfi [--dry-run]
    stoxsy alerts [run]                 open alerts, or re-evaluate them
    stoxsy backup [--out DIR]           compressed database dump (pg_dump)
    stoxsy restore --dump FILE --yes    restore a dump into the configured database (destructive: needs --yes)

It operates the system; the web UI is where charts and decisions are read. Nothing here places an order.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from qsde.config import paths

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
STREAM = [str(SCRIPTS / "stream_run.py"), "--mode", "once", "--enable-network", "--sink", "db"]
INGEST = {
    "daily": [sys.executable, str(SCRIPTS / "daily_after_close.py")],
    "deals": [sys.executable, *STREAM, "--source", "nse_deals"],
    "fno": [sys.executable, *STREAM, "--source", "nse_fo_bhavcopy"],
    "nse": [sys.executable, *STREAM, "--source", "bhavcopy"],
    "bse": [sys.executable, *STREAM, "--source", "bse_bhavcopy"],
    "amfi": [sys.executable, *STREAM, "--source", "amfi_navall"],
}


def _print(*a) -> None:
    print(*a, flush=True)


def cmd_doctor(args) -> int:
    from qsde.ops import checks

    cs = checks.run_all()
    _print(checks.render(cs))
    return checks.exit_code(cs)


def cmd_status(args) -> int:
    from qsde.ops import checks

    cs = [c for c in checks.run_all() if c.name not in ("runtime",)]
    _print(f"Stoxsy status, {datetime.now():%Y-%m-%d %H:%M}")
    _print(checks.render(cs))
    return checks.exit_code(cs)


def cmd_heartbeat(args) -> int:
    from qsde.ops import checks

    cs = checks.heartbeats()
    _print(checks.render(cs))
    return checks.exit_code(cs)


def cmd_coverage(args) -> int:
    from qsde.ops import coverage as C

    rows = C.gather()
    _print(C.render_text(rows))
    out = Path(args.out) if args.out else paths.data_path("reports", "coverage")
    out.mkdir(parents=True, exist_ok=True)
    today = date.today()
    (out / f"coverage_{today}.md").write_text(C.render_markdown(rows, today), encoding="utf-8")
    (out / f"coverage_{today}.html").write_text(C.render_html(rows, today), encoding="utf-8")
    _print(f"\nreport: {out / f'coverage_{today}.html'}")
    return 0


def cmd_ingest(args) -> int:
    argv = INGEST[args.what]
    _print("run: " + " ".join(argv))
    if args.dry_run:
        return 0
    if args.what == "daily":
        argv = argv + (["--dry-run"] if args.dry_run else [])
    return subprocess.run(argv, cwd=str(SCRIPTS.parent)).returncode


def cmd_alerts(args) -> int:
    if args.action == "run":
        return subprocess.run([sys.executable, str(SCRIPTS / "run_alerts.py")], cwd=str(SCRIPTS.parent)).returncode
    from qsde.db.connection import read_sql

    d = read_sql("SELECT severity, title, state, occurrences, last_seen FROM alert_events WHERE state <> 'resolved' ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END, last_seen DESC")
    if d.empty:
        _print("no open alerts")
        return 0
    for r in d.itertuples():
        _print(f"{r.severity.upper():8} {r.state:12} {r.title}  (seen {r.occurrences}x, last {r.last_seen:%Y-%m-%d %H:%M})")
    return 1 if (d.severity == "critical").any() else 0


def _pg_args() -> dict:
    from qsde.config.settings import settings

    u = urlparse(settings.database_url.replace("postgresql+psycopg2", "postgresql"))
    return {"host": u.hostname or "127.0.0.1", "port": str(u.port or 5432), "user": u.username or "qsde", "password": u.password or "", "db": (u.path or "/qsde").lstrip("/")}


def _pg_cmd(tool: str, extra: list, c: dict) -> Optional[list]:
    """Prefer a client on PATH; fall back to the TimescaleDB container."""
    if shutil.which(tool):
        return [tool, "-h", c["host"], "-p", c["port"], "-U", c["user"], *extra]
    if shutil.which("docker"):
        return ["docker", "exec", "-i", "-e", f"PGPASSWORD={c['password']}", "qsde_timescaledb", tool, "-U", c["user"], *extra]
    return None


def cmd_backup(args) -> int:
    c = _pg_args()
    out = Path(args.out) if args.out else paths.data_path("backup")
    out.mkdir(parents=True, exist_ok=True)
    f = out / f"stoxsy_{datetime.now():%Y%m%d_%H%M}.dump"
    cmd = _pg_cmd("pg_dump", ["-d", c["db"], "-Fc", "-Z", "6"], c)
    if cmd is None:
        _print("neither pg_dump nor docker is available")
        return 2
    import os

    env = {**os.environ, "PGPASSWORD": c["password"]}
    _print("dumping to " + str(f))
    with f.open("wb") as fh:
        r = subprocess.run(cmd, stdout=fh, env=env)
    if r.returncode != 0 or f.stat().st_size < 1000:
        _print("backup FAILED")
        f.unlink(missing_ok=True)
        return 2
    _print(f"backup ok: {f} ({f.stat().st_size / 1e6:,.0f} MB). Test it: restore into a scratch database before you rely on it.")
    return 0


def cmd_restore(args) -> int:
    if not args.yes:
        _print("restore replaces data in the configured database. Re-run with --yes to proceed.")
        return 2
    dump = Path(args.dump)
    if not dump.exists():
        _print(f"{dump} not found")
        return 2
    c = _pg_args()
    cmd = _pg_cmd("pg_restore", ["-d", c["db"], "--clean", "--if-exists", "--no-owner"], c)
    if cmd is None:
        _print("neither pg_restore nor docker is available")
        return 2
    import os

    env = {**os.environ, "PGPASSWORD": c["password"]}
    with dump.open("rb") as fh:
        r = subprocess.run(cmd, stdin=fh, env=env)
    _print("restore finished (exit %d); run `stoxsy doctor` to verify" % r.returncode)
    return 0 if r.returncode == 0 else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="stoxsy", description="Stoxsy operator console. Research and decision support; nothing here places an order.")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="one-screen summary").set_defaults(fn=cmd_status)
    sub.add_parser("doctor", help="all health checks (exit 0 ok, 1 warn, 2 fail)").set_defaults(fn=cmd_doctor)
    sub.add_parser("heartbeat", help="liveness of the stream and the daily routine").set_defaults(fn=cmd_heartbeat)
    c = sub.add_parser("coverage", help="data-coverage matrix")
    c.add_argument("--out", default=None)
    c.set_defaults(fn=cmd_coverage)
    i = sub.add_parser("ingest", help="run a data load")
    i.add_argument("what", choices=sorted(INGEST))
    i.add_argument("--dry-run", action="store_true")
    i.set_defaults(fn=cmd_ingest)
    a = sub.add_parser("alerts", help="open alerts, or `alerts run` to re-evaluate")
    a.add_argument("action", nargs="?", choices=["list", "run"], default="list")
    a.set_defaults(fn=cmd_alerts)
    b = sub.add_parser("backup", help="compressed database dump")
    b.add_argument("--out", default=None)
    b.set_defaults(fn=cmd_backup)
    r = sub.add_parser("restore", help="restore a dump (destructive)")
    r.add_argument("--dump", required=True)
    r.add_argument("--yes", action="store_true")
    r.set_defaults(fn=cmd_restore)
    return p


def main(argv: Optional[list] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.fn(args) or 0)
    except KeyboardInterrupt:
        return 130
    except Exception as e:                                           # noqa: BLE001
        _print(f"stoxsy {args.cmd}: {type(e).__name__}: {e}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
