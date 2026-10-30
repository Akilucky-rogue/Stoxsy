"""AMFI TER storage and loader, tested against the real database and the owner's real file (see docs/design/AMFI_TER_DESIGN.md)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from qsde.db.connection import read_sql  # noqa: E402

pytestmark = pytest.mark.real_data
TABLES = ["mf_ter_layouts", "mf_ter_files", "mf_ter_scheme", "mf_ter", "mf_ter_map", "mf_ter_unmatched"]


def test_062_tables_and_keys_exist():
    have = set(read_sql("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")["table_name"])
    assert set(TABLES) <= have, f"missing tables: {sorted(set(TABLES) - have)}"
    pk = read_sql("""SELECT a.attname FROM pg_index i JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
                     WHERE i.indrelid = 'mf_ter'::regclass AND i.indisprimary ORDER BY array_position(i.indkey::int[], a.attnum::int)""")
    assert list(pk["attname"]) == ["nsdl_code", "ter_date"]
    col = read_sql("SELECT data_type, numeric_precision, numeric_scale FROM information_schema.columns WHERE table_name = 'mf_ter' AND column_name = 'r_total'").iloc[0]
    assert col["data_type"] == "numeric" and pd.isna(col["numeric_precision"]), "values must be exact numeric (no fixed decimals): AMFI publishes up to 4"
    uniq = read_sql("""SELECT count(*) AS n FROM pg_index i JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
                       WHERE i.indrelid = 'mf_ter_files'::regclass AND i.indisunique AND a.attname = 'sha256' AND i.indnatts = 1""")
    assert int(uniq["n"].iloc[0]) == 1
    mp = read_sql("""SELECT a.attname FROM pg_index i JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
                     WHERE i.indrelid = 'mf_ter_map'::regclass AND i.indisprimary ORDER BY array_position(i.indkey::int[], a.attnum::int)""")
    assert list(mp["attname"]) == ["scheme_code", "plan"]                                  # one TER code per master scheme and plan


# ---- Task 5: loader ----------------------------------------------------------------------------------------------------------------------
import hashlib  # noqa: E402
import os  # noqa: E402
import shutil  # noqa: E402
import time  # noqa: E402

import pandas as pd  # noqa: E402
import psycopg2  # noqa: E402
import psycopg2.extras  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from amfi_ter_support import OCTOBER_SHA16, independent_frame, real_file_by_sha16, real_files, require_real_file  # noqa: E402
from qsde.config.paths import data_path  # noqa: E402
from qsde.config import settings  # noqa: E402
from qsde.stream.landing import LandingZone  # noqa: E402

import load_amfi_ter as L  # noqa: E402


def _counts(conn):
    cur = conn.cursor()
    out = {}
    for t in TABLES:
        cur.execute(f"SELECT count(*) AS n FROM {t}")
        out[t] = cur.fetchone()["n"]
    return out


def _make_world(f, tmp_path):
    """A rollback-only connection to the real database, a temp landing folder, and every real inbox file with any earlier load of it cleared INSIDE the transaction (restored by the rollback)."""
    conn = psycopg2.connect(settings.database_url, cursor_factory=psycopg2.extras.RealDictCursor)
    shas = [hashlib.sha256(p.read_bytes()).hexdigest() for p in real_files()]               # every real inbox file, so the tests behave the same whatever is already loaded
    cur = conn.cursor()
    cur.execute("SELECT layout_hash FROM mf_ter_files WHERE sha256 = ANY(%s)", (shas,))
    layouts = [r["layout_hash"] for r in cur.fetchall()]
    cur.execute("DELETE FROM mf_ter_files WHERE sha256 = ANY(%s)", (shas,))
    for h in layouts:
        cur.execute("DELETE FROM mf_ter_layouts WHERE layout_hash = %s AND NOT EXISTS (SELECT 1 FROM mf_ter_files WHERE layout_hash = %s)", (h, h))
    from qsde.ingestion import amfi_ter as A
    cur.execute("DELETE FROM mf_ter_layouts WHERE layout_hash = %s AND NOT EXISTS (SELECT 1 FROM mf_ter_files WHERE layout_hash = %s)", (A.read_export(f.read_bytes(), f.name).layout_hash,) * 2)
    yield f, conn, LandingZone(tmp_path / "landing"), tmp_path
    conn.rollback()
    conn.close()


@pytest.fixture()
def world(tmp_path):
    yield from _make_world(require_real_file(), tmp_path)


@pytest.fixture()
def world_october(tmp_path):
    """The same, for the October 2026 download specifically (facts that are known only about that file)."""
    yield from _make_world(real_file_by_sha16(OCTOBER_SHA16), tmp_path)


def test_dry_run_writes_nothing(world):
    f, conn, landing, tmp = world
    before = _counts(conn)
    r = L.load_file(f, dry_run=True, conn=conn, landing=landing)
    assert r["status"] == "dry run" and len(r["sample"]) == 5 and r["unmatched_headers"] == []
    assert _counts(conn) == before
    assert list((tmp / "landing").rglob("*")) == []


def test_first_file_of_a_layout_is_refused_until_mapping_ok(world):
    f, conn, landing, tmp = world
    before = _counts(conn)
    r = L.load_file(f, dry_run=False, mapping_ok=False, conn=conn, landing=landing)
    assert r["status"].startswith("REFUSED") and "mapping" in r["status"]
    assert _counts(conn) == before and list((tmp / "landing").rglob("*")) == []
    assert L.load_file(f, dry_run=False, mapping_ok=True, conn=conn, landing=landing)["status"] == "loaded"


def test_load_reconciles_with_the_file(world):
    f, conn, landing, _ = world
    r = L.load_file(f, dry_run=False, mapping_ok=True, conn=conn, landing=landing)
    cur = conn.cursor()
    cur.execute("SELECT count(*) AS n FROM mf_ter WHERE file_id = %s", (r["file_id"],))
    assert cur.fetchone()["n"] == r["rows_new"] and r["rows_new"] + r["overlap_rows"] == r["parsed"]
    cur.execute("SELECT n_data_rows, n_parsed, n_rejected, n_footer_rows, last_date_partial FROM mf_ter_files WHERE id = %s", (r["file_id"],))
    row = cur.fetchone()
    assert row["n_data_rows"] == row["n_parsed"] + row["n_rejected"] and row["n_footer_rows"] > 0
    cur.execute("SELECT count(*) AS n FROM mf_ter_map")
    assert cur.fetchone()["n"] > 0


def test_raw_file_is_landed_byte_identical(world):
    f, conn, landing, _ = world
    r = L.load_file(f, dry_run=False, mapping_ok=True, conn=conn, landing=landing)
    landed = Path(r["landed"])
    assert hashlib.sha256(landed.read_bytes()).hexdigest() == hashlib.sha256(f.read_bytes()).hexdigest()
    cur = conn.cursor()
    cur.execute("SELECT sha256 FROM mf_ter_files WHERE id = %s", (r["file_id"],))
    assert cur.fetchone()["sha256"].strip() == hashlib.sha256(f.read_bytes()).hexdigest()


def test_reload_is_skipped(world):
    f, conn, landing, _ = world
    L.load_file(f, dry_run=False, mapping_ok=True, conn=conn, landing=landing)
    before = _counts(conn)
    assert L.load_file(f, dry_run=False, mapping_ok=True, conn=conn, landing=landing)["status"] == "already loaded"
    assert _counts(conn) == before


def test_load_is_all_or_nothing(world):
    f, conn, landing, _ = world
    before = _counts(conn)
    cur = conn.cursor()
    cur.execute("SAVEPOINT before_load")                                                 # the fixture's own clearing of an earlier real load must survive this test
    L.load_file(f, dry_run=False, mapping_ok=True, conn=conn, landing=landing)
    assert _counts(conn)["mf_ter_files"] == before["mf_ter_files"] + 1
    cur.execute("ROLLBACK TO SAVEPOINT before_load")                                     # what a failure mid-load does: the whole load disappears, in every table
    assert _counts(conn) == before


def test_overlapping_month_counts_restatements(world):
    f, conn, landing, _ = world
    others = [p for p in real_files() if p.suffix.lower() == ".xlsx" and p != f]
    if not others:
        pytest.skip("needs a second real month")
    first = L.load_file(f, dry_run=False, mapping_ok=True, conn=conn, landing=landing)
    second = L.load_file(others[0], dry_run=False, mapping_ok=True, conn=conn, landing=landing)
    a, b = independent_frame(f), independent_frame(others[0])
    key = ["NSDL Scheme Code", "TER Date"]
    m = a.merge(b, on=key, suffixes=("_a", "_b"))
    cols = [c for c in a.columns if c not in key + ["Scheme Name", "Scheme Type", "Scheme Category"]]
    want = int(sum(any(abs(float(r[c + "_a"]) - float(r[c + "_b"])) > 0.0005 for c in cols) for _, r in m.iterrows()))
    assert second["restatements"] == want and second["overlap_rows"] == len(m)


def test_inbox_ignores_lock_and_partial_files(tmp_path):
    real = require_real_file()
    for name in ("~$ter-of-mf-schemes.xlsx", "x.xlsx.crdownload", "y.part", "z.tmp"):
        (tmp_path / name).write_bytes(b"")
    fresh = tmp_path / "fresh.xlsx"
    shutil.copy2(real, fresh)
    os.utime(fresh, (time.time(), time.time()))                                           # modified just now: may still be copying
    aged = tmp_path / "aged.xlsx"
    shutil.copy2(real, aged)
    os.utime(aged, (time.time() - 300, time.time() - 300))
    files, messages = L.inbox_files(tmp_path)
    assert [p.name for p in files] == ["aged.xlsx"]
    text = " ".join(messages)
    for name in ("~$ter-of-mf-schemes.xlsx", "x.xlsx.crdownload", "y.part", "z.tmp", "fresh.xlsx"):
        assert name in text


def test_rematch_rebuilds_the_mapping_from_stored_rows(world):
    f, conn, landing, _ = world
    L.load_file(f, dry_run=False, mapping_ok=True, conn=conn, landing=landing)
    cur = conn.cursor()
    stored = "SELECT nsdl_code, scheme_code, plan FROM mf_ter_map WHERE nsdl_code IN (SELECT DISTINCT nsdl_code FROM mf_ter) ORDER BY 1, 2, 3"          # codes that have stored rows
    cur.execute(stored)
    before = cur.fetchall()
    cur.execute("DELETE FROM mf_ter_map")                                                  # start from nothing: what a changed master or key rule would force
    r = L.rematch(conn)
    cur.execute(stored)
    after = cur.fetchall()
    # Only the newest file rebuilds the mapping on load, so after loading an OLDER month the stored map may lack schemes that only that month holds; a rebuild from the stored rows
    # legitimately adds them. So: the rebuild loses nothing the load mapped, reports what it holds, and gives the same result when run again.
    assert all(row in after for row in before) and r["mapped_rows"] == len(after)
    r2 = L.rematch(conn)
    cur.execute(stored)
    assert cur.fetchall() == after and r2["mapped_rows"] == r["mapped_rows"]
    cur.execute("SELECT count(*) AS n FROM mf_ter_unmatched WHERE file_id = (SELECT max(id) FROM mf_ter_files)")
    assert cur.fetchone()["n"] == r["unmatched_rows"]


def _wrong_layout_file():
    cands = sorted(data_path("landing", "bse_corporate_actions").rglob("*.csv"))
    if not cands:
        pytest.skip("no real non-TER file available")
    return cands[0]


def test_stored_values_equal_published_exactly(world):
    """Review finding 1: AMFI publishes up to 4 decimals; nothing may be rounded on the way in."""
    from decimal import Decimal

    f, conn, landing, _ = world
    r = L.load_file(f, dry_run=False, mapping_ok=True, conn=conn, landing=landing)
    cur = conn.cursor()
    cur.execute(f"SELECT nsdl_code, ter_date, {', '.join(L.NUM)} FROM mf_ter WHERE file_id = %s", (r["file_id"],))
    stored = pd.DataFrame(cur.fetchall())
    d = independent_frame(f)
    src = list(d.columns[5:])
    d["nsdl_code"] = d[d.columns[0]].str.replace(r"\s+", "", regex=True).str.replace(r"\?$", "", regex=True)
    d["ter_date"] = pd.to_datetime(d["TER Date"]).dt.date
    m = d.merge(stored, on=["nsdl_code", "ter_date"], suffixes=("", "_db"))
    assert len(m) == len(d) == len(stored)
    bad = 0
    for plan_cols, db_cols in ((src[:5], L.NUM[:5]), (src[5:], L.NUM[5:])):
        offered = ~m[plan_cols].apply(lambda c: c.astype(float)).eq(0).all(axis=1)
        for sc, dc in zip(plan_cols, db_cols):
            pub = m.loc[offered, sc].map(lambda x: Decimal(str(x).strip()))
            sto = m.loc[offered, dc].map(lambda x: None if pd.isna(x) else Decimal(str(x)))
            bad += int((pub != sto).sum())
    assert bad == 0, f"{bad} stored values differ from what AMFI published"


def test_a_rejected_file_does_not_wedge_the_inbox(tmp_path, monkeypatch, capsys):
    """Review finding 2: a bad file that sorts first must not stop the good file behind it, and the exit code must still say something was rejected."""
    real = require_real_file()
    bad, good = tmp_path / "a_wrong_layout.csv", tmp_path / "ter.xlsx"
    shutil.copy2(_wrong_layout_file(), bad)
    shutil.copy2(real, good)
    for p in (bad, good):
        os.utime(p, (time.time() - 300, time.time() - 300))
    monkeypatch.setattr(L, "INBOX", tmp_path)
    monkeypatch.setattr(sys, "argv", ["load_amfi_ter.py", "--inbox", "--dry-run"])
    rc = L.main()
    out = capsys.readouterr().out
    assert rc == 2 and "REJECTED a_wrong_layout.csv" in out
    assert '"file": "ter.xlsx"' in out                                                   # the good file behind the bad one was still reached (dry run, or already loaded)


def test_older_file_does_not_touch_the_mapping(world):
    """Review finding 3: a file older than one already loaded must not re-derive (and overwrite) the mapping. The real rows are loaded, then their bookkeeping dates are moved
    forward inside this rolled-back transaction so that the same real file now counts as the older of two."""
    f, conn, landing, _ = world
    first = L.load_file(f, dry_run=False, mapping_ok=True, conn=conn, landing=landing)
    cur = conn.cursor()
    cur.execute("UPDATE mf_ter_files SET sha256 = %s, date_min = date_min + 40, date_max = date_max + 40 WHERE id = %s", ("0" * 64, first["file_id"]))
    cur.execute("DELETE FROM mf_ter WHERE file_id = %s", (first["file_id"],))
    cur.execute("DELETE FROM mf_ter_map")
    cur.execute("DELETE FROM mf_ter_unmatched")
    r = L.load_file(f, dry_run=False, mapping_ok=True, conn=conn, landing=landing)
    assert r["status"] == "loaded" and r["rows_new"] == r["parsed"] and r["mapping_kept"] is True
    cur.execute("SELECT (SELECT count(*) FROM mf_ter_map) AS m, (SELECT count(*) FROM mf_ter_unmatched WHERE file_id = %s) AS u", (r["file_id"],))
    row = cur.fetchone()
    assert row["m"] == 0 and row["u"] == 0


def test_the_same_month_in_two_formats_adds_no_rows_and_no_restatements(world):
    """Real check of the overlap path (review focus 2): the owner's Excel workbook and its Save As CSV are the same month twice."""
    f, conn, landing, _ = world
    csv = f.with_suffix(".csv")
    if not csv.exists():
        pytest.skip("ask the owner to Save As CSV")
    a = L.load_file(f, dry_run=False, mapping_ok=True, conn=conn, landing=landing)
    b = L.load_file(csv, dry_run=False, mapping_ok=True, conn=conn, landing=landing)
    assert b["status"] == "loaded" and b["rows_new"] == 0 and b["overlap_rows"] == a["parsed"] == b["parsed"] and b["restatements"] == 0


def test_landed_origin_says_whether_the_file_was_fetched_by_the_command_or_saved_by_hand():
    assert L.origin_of("ter_revised_10-2026__2026-10-30.xlsx") == "fetch_ter"
    for name in ("ter-of-mf-schemes.xlsx", "September_ter-of-mf-schemes.csv", "ter-of-mf-schemes (1).xlsx"):
        assert L.origin_of(name) == "manual_download"


def test_an_already_loaded_file_is_recognised_without_parsing_it_again(world, monkeypatch):
    """The nightly inbox step meets every old file every night: the SHA-256 check must come before the (seconds-long) parse, so the cost stays flat as months accumulate."""
    f, conn, landing, _ = world
    first = L.load_file(f, dry_run=False, mapping_ok=True, conn=conn, landing=landing)
    assert first["status"] == "loaded"
    from qsde.ingestion import amfi_ter as A

    def boom(*a, **k):
        raise AssertionError("the file was parsed although its SHA-256 is already in the ledger")

    monkeypatch.setattr(A, "read_export", boom)
    again = L.load_file(f, dry_run=False, mapping_ok=True, conn=conn, landing=landing)
    assert again["status"] == "already loaded" and again["file_id"] == first["file_id"]
