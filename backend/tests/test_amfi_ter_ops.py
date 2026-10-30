"""AMFI TER coverage, report, doctor line and daily step, tested on the owner's real file loaded inside a rolled-back transaction (see docs/design/AMFI_TER_DESIGN.md)."""
import hashlib
import re
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from amfi_ter_support import real_files  # noqa: E402
from test_amfi_ter_loader import world  # noqa: E402,F401  (the real-file / rollback-only connection fixture)
from qsde.ingestion import amfi_ter as A  # noqa: E402
from qsde.ingestion import amfi_ter_db as D  # noqa: E402
from qsde.ops import checks as C  # noqa: E402

import daily_after_close as DAILY  # noqa: E402
import load_amfi_ter as L  # noqa: E402

pytestmark = pytest.mark.real_data
OCTOBER_SHA16 = "0C1DD64F0A4EB231"
TODAY = date(2026, 10, 30)


def _loaded(world):
    f, conn, landing, tmp = world
    L.load_file(f, dry_run=False, mapping_ok=True, conn=conn, landing=landing)
    ex = A.read_export(f.read_bytes(), f.name)
    return f, conn, ex, tmp


def _latest_complete(ex):
    partial = set(A.partial_dates(ex.frame))
    return max(d for d in ex.frame["ter_date"].dt.date.unique() if d not in partial)


def test_coverage_reproduces_by_hand(world):
    f, conn, ex, _ = _loaded(world)
    cov = D.coverage(conn, TODAY)
    latest = _latest_complete(ex)
    cur = conn.cursor()
    cur.execute("SELECT scheme_code, name, amc, category_raw, is_direct, is_growth, last_nav_date FROM mf_scheme_master")
    master = pd.DataFrame(cur.fetchall())
    scope = master[pd.to_datetime(master["last_nav_date"]) >= pd.Timestamp(TODAY) - pd.Timedelta(days=90)]
    match = A.match_schemes(A.ter_schemes(ex.frame), master, ex.frame["ter_date"].max().date())
    day = ex.frame[ex.frame["ter_date"].dt.date == latest].set_index("nsdl_code")
    has_value = match.mapped.apply(lambda r: r["nsdl_code"] in day.index and pd.notna(day.loc[r["nsdl_code"], "r_total" if r["plan"] == "regular" else "d_total"]), axis=1)
    mapped = set(match.mapped[has_value]["scheme_code"]) & set(scope["scheme_code"])
    assert cov["latest_complete_date"] == latest
    assert cov["in_scope"] == len(scope) and cov["mapped"] == len(mapped)
    assert cov["pct"] == round(100 * len(mapped) / len(scope), 1)
    loaded_shas = {hashlib.sha256(f.read_bytes()).hexdigest()}
    cur.execute("SELECT sha256 FROM mf_ter_files")
    loaded_shas |= {r["sha256"].strip() for r in cur.fetchall()}
    waiting = [p for p in real_files() if hashlib.sha256(p.read_bytes()).hexdigest() not in loaded_shas]
    assert cov["files_waiting"] == len(waiting)


def test_latest_complete_date_excludes_the_partial_day(world):
    _, conn, ex, _ = _loaded(world)
    if ex.sha256[:16].upper() != OCTOBER_SHA16:
        pytest.skip("not the October 2026 file")
    assert D.coverage(conn, TODAY)["latest_complete_date"] == date(2026, 10, 28)


def test_every_gap_has_a_reason(world):
    _, conn, _, _ = _loaded(world)
    cov = D.coverage(conn, TODAY)
    assert cov["by_reason"] and sum(cov["by_reason"].values()) == cov["in_scope"] - cov["mapped"]


def test_report_contains_no_value_table(world):
    _, conn, _, tmp = _loaded(world)
    path = D.write_report(D.report_info(conn, TODAY), out_dir=tmp / "reports")
    text = path.read_text(encoding="utf-8")
    assert "coverage" in text.lower() and "unmatched" in text.lower()
    assert not any(len(re.findall(r"\d+\.\d{1,3}", line)) >= 5 for line in text.splitlines()), "the report must not reproduce AMFI's TER values"
    assert "mf_ter" not in str(path) and str(path).startswith(str(tmp))


def test_doctor_line(world):
    _, conn, ex, _ = _loaded(world)
    cov = D.coverage(conn, TODAY)
    now = C.check_fund_ter(today=TODAY, conn=conn)
    assert now.name == "fund expense ratios" and now.level == ("warn" if cov["files_waiting"] else "ok")
    stale = C.check_fund_ter(today=cov["latest_complete_date"] + timedelta(days=60), conn=conn)
    assert stale.level == "warn" and "days old" in stale.message


def test_daily_step_is_registered_before_the_fund_snapshot():
    names = [s[0] for s in DAILY.STEPS]
    assert "amfi_ter_inbox" in names and names.index("amfi_ter_inbox") < names.index("fund_snapshot")
    step = DAILY.STEPS[names.index("amfi_ter_inbox")]
    assert step[1] is False and step[3][-1] == "--inbox" and step[3][-2].endswith("load_amfi_ter.py")
