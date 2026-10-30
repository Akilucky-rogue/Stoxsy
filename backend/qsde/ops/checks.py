"""Health checks for the operator console (`stoxsy doctor`, `stoxsy status`). Each check returns a Check(level, name, message); `level` is ok / warn / fail so a monitor can use the exit code.
Checks never raise: a check that cannot run is reported as a failure of that check."""
from __future__ import annotations

import os
import platform
import shutil
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable, Optional

import pandas as pd

from qsde.config import paths

LEVELS = ("ok", "warn", "fail")
MIGRATIONS = Path(__file__).resolve().parents[3] / "infra" / "migrations"


@dataclass
class Check:
    level: str
    name: str
    message: str


def _safe(name: str, fn: Callable[[], Check]) -> Check:
    try:
        return fn()
    except Exception as e:                                           # noqa: BLE001
        return Check("fail", name, f"could not run: {type(e).__name__}: {str(e)[:160]}")


def check_runtime() -> Check:
    return Check("ok", "runtime", f"Python {platform.python_version()} on {platform.system()} {platform.release()}; root {paths.ROOT}; data {paths.DATA}")


def check_paths() -> Check:
    d = paths.DATA
    if not d.exists():
        return Check("fail", "data directory", f"{d} does not exist (set STOXSY_ROOT or STOXSY_DATA_DIR)")
    probe = d / ".write_probe"
    try:
        probe.write_text("x")
        probe.unlink()
    except OSError as e:
        return Check("fail", "data directory", f"{d} is not writable: {e}")
    return Check("ok", "data directory", f"{d} is writable")


def check_disk(warn_gb: float = 20.0, fail_gb: float = 5.0) -> Check:
    free = shutil.disk_usage(paths.DATA if paths.DATA.exists() else Path.home()).free / 1e9
    lvl = "fail" if free < fail_gb else "warn" if free < warn_gb else "ok"
    return Check(lvl, "disk space", f"{free:,.0f} GB free (warn below {warn_gb:.0f}, fail below {fail_gb:.0f})")


def check_database(read_sql=None) -> Check:
    from qsde.db.connection import read_sql as _rs

    rs = read_sql or _rs
    v = rs("SELECT version() AS v, (SELECT extversion FROM pg_extension WHERE extname = 'timescaledb') AS ts").iloc[0]
    n = len(list(MIGRATIONS.glob("*.sql")))
    ts = f"TimescaleDB {v['ts']}" if v["ts"] else "no TimescaleDB extension"
    return Check("ok" if v["ts"] else "warn", "database", f"{str(v['v']).split(',')[0]}; {ts}; {n} migration files in the repository")


def freshness(read_sql=None, now: Optional[pd.Timestamp] = None) -> list:
    from qsde.alerts import facts as F
    from qsde.alerts import rules as R

    cfg = R.load_config()["data_staleness"]
    now = now or pd.Timestamp.now(tz="UTC")
    exp = R.expected_last_session(now.tz_convert("Asia/Kolkata"), cfg["after_hours_ist"])
    d = F.data_dates()
    out = []
    for key, label in (("nse_last", "NSE prices"), ("bse_last", "BSE prices"), ("fno_last", "F&O archive")):
        last = d.get(key)
        if last is None:
            out.append(Check("fail", label, "no data"))
            continue
        behind = R.weekdays_behind(last, exp)
        out.append(Check("fail" if behind >= cfg["nse_weekdays_critical"] else "warn" if behind >= cfg["nse_weekdays_warn"] else "ok", label, f"latest {last}, {behind} weekday(s) behind the expected {exp}"))
    am = d.get("amfi_last")
    if am is not None:
        days = (now.tz_convert("Asia/Kolkata").date() - am).days
        out.append(Check("fail" if days >= cfg["amfi_days_critical"] else "warn" if days >= cfg["amfi_days_warn"] else "ok", "fund NAVs", f"latest {am}, {days} day(s) old"))
    return out


def heartbeats(now: Optional[pd.Timestamp] = None) -> list:
    from qsde.ops import heartbeat

    now = now or pd.Timestamp.now(tz="UTC")
    rows = {r["component"]: r for r in heartbeat.read_all()}
    out = []
    for comp, warn_min in (("stream", 5), ("daily_pipeline", 36 * 60)):
        r = rows.get(comp)
        if r is None:
            out.append(Check("warn", f"heartbeat {comp}", "never reported on this database"))
            continue
        age = (now - pd.Timestamp(r["last_beat"])).total_seconds() / 60
        bad = r["status"] in ("failed", "degraded")
        stale = age > warn_min and r["status"] != "stopped"
        lvl = "fail" if r["status"] == "failed" else "warn" if (bad or stale) else "ok"
        out.append(Check(lvl, f"heartbeat {comp}", f"{r['status']} on {r['host']}, last beat {age:,.0f} min ago"))
    return out


def check_kite() -> Check:
    from qsde.alerts import facts as F

    k = F.kite()
    if not k["has_token"]:
        return Check("warn", "Kite session", "no active token: log in to enable live data")
    left = (pd.Timestamp(k["expires_at"]) - pd.Timestamp.now(tz="UTC")).total_seconds() / 3600
    return Check("fail" if left <= 0 else "warn" if left < 2 else "ok", "Kite session", f"token valid for {left:.1f} h" if left > 0 else "token expired: log in again")


def check_calendar() -> Check:
    from qsde import exchange_calendar

    st = exchange_calendar.load().status(pd.Timestamp.now(tz="Asia/Kolkata").date())
    return Check("ok" if st["state"] == "ok" else "fail" if st["state"] in ("missing", "expired") else "warn", "exchange calendar", st["message"])


def check_alerts() -> Check:
    from qsde.db.connection import read_sql

    d = read_sql("SELECT severity, count(*) AS n FROM alert_events WHERE state IN ('open', 'acknowledged') GROUP BY 1")
    n = {r.severity: int(r.n) for r in d.itertuples()}
    crit = n.get("critical", 0)
    return Check("fail" if crit else "warn" if n.get("warning", 0) else "ok", "alerts", f"{crit} critical, {n.get('warning', 0)} warning, {n.get('info', 0)} info open")


def check_fund_ter(today=None, conn=None) -> Check:
    """Fund expense ratios (AMFI TER, manual download): latest complete date, coverage of active schemes, files waiting. Never FAIL: the data is loaded by hand."""
    from qsde.ingestion import amfi_ter_db as D

    today = today or pd.Timestamp.now(tz="Asia/Kolkata").date()
    if conn is not None:
        cov = D.coverage(conn, today)
    else:
        from qsde.db.connection import get_sync_conn

        with get_sync_conn() as c:
            cov = D.coverage(c, today)
    waiting = f"; {cov['files_waiting']} file(s) waiting in {D.INBOX} (run load_amfi_ter.py --inbox --dry-run)" if cov["files_waiting"] else ""
    d = cov["latest_complete_date"]
    if d is None:
        return Check("warn", "fund expense ratios", f"no expense ratios loaded: download a month from AMFI's TER page into {D.INBOX}{waiting}")
    age = (today - d).days
    msg = f"latest complete {d}, {age} days old; {cov['mapped']} of {cov['in_scope']} active schemes covered ({cov['pct']}%){waiting}"
    return Check("warn" if age > D.STALE_DAYS or cov["files_waiting"] else "ok", "fund expense ratios", msg)


def run_all() -> list:
    out = [_safe("runtime", check_runtime), _safe("data directory", check_paths), _safe("disk space", check_disk), _safe("database", check_database)]
    for name, fn in (("data freshness", freshness), ("heartbeats", heartbeats)):
        try:
            out += fn()
        except Exception as e:                                       # noqa: BLE001
            out.append(Check("fail", name, f"could not run: {type(e).__name__}: {str(e)[:160]}"))
    out += [_safe("fund expense ratios", check_fund_ter), _safe("Kite session", check_kite), _safe("exchange calendar", check_calendar), _safe("alerts", check_alerts)]
    return out


def exit_code(checks: list) -> int:
    return 2 if any(c.level == "fail" for c in checks) else 1 if any(c.level == "warn" for c in checks) else 0


def render(checks: list, color: Optional[bool] = None) -> str:
    color = sys.stdout.isatty() if color is None else color
    tag = {"ok": "\033[32mOK  \033[0m", "warn": "\033[33mWARN\033[0m", "fail": "\033[31mFAIL\033[0m"} if color else {"ok": "OK  ", "warn": "WARN", "fail": "FAIL"}
    w = max(len(c.name) for c in checks)
    return "\n".join(f"{tag[c.level]}  {c.name.ljust(w)}  {c.message}" for c in checks)
