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
    assert (col["data_type"], int(col["numeric_precision"]), int(col["numeric_scale"])) == ("numeric", 7, 3)
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
from amfi_ter_support import independent_frame, real_files, require_real_file  # noqa: E402
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


@pytest.fixture()
def world(tmp_path):
    """A rollback-only connection to the real database, a temp landing folder, and the real file with any earlier load of it cleared INSIDE the transaction (restored by the rollback)."""
    f = require_real_file()
    conn = psycopg2.connect(settings.database_url, cursor_factory=psycopg2.extras.RealDictCursor)
    sha = hashlib.sha256(f.read_bytes()).hexdigest()
    cur = conn.cursor()
    cur.execute("SELECT layout_hash FROM mf_ter_files WHERE sha256 = %s", (sha,))
    layouts = [r["layout_hash"] for r in cur.fetchall()]
    cur.execute("DELETE FROM mf_ter_files WHERE sha256 = %s", (sha,))
    for h in layouts:
        cur.execute("DELETE FROM mf_ter_layouts WHERE layout_hash = %s AND NOT EXISTS (SELECT 1 FROM mf_ter_files WHERE layout_hash = %s)", (h, h))
    from qsde.ingestion import amfi_ter as A
    cur.execute("DELETE FROM mf_ter_layouts WHERE layout_hash = %s AND NOT EXISTS (SELECT 1 FROM mf_ter_files WHERE layout_hash = %s)", (A.read_export(f.read_bytes(), f.name).layout_hash,) * 2)
    yield f, conn, LandingZone(tmp_path / "landing"), tmp_path
    conn.rollback()
    conn.close()


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
    L.load_file(f, dry_run=False, mapping_ok=True, conn=conn, landing=landing)
    assert _counts(conn)["mf_ter_files"] == before["mf_ter_files"] + 1
    conn.rollback()                                                                      # what a failure mid-load does: the whole load disappears
    after = _counts(conn)
    assert all(after[t] <= before[t] for t in TABLES) and after["mf_ter_files"] == before["mf_ter_files"]


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
    cur.execute("SELECT nsdl_code, scheme_code, plan FROM mf_ter_map ORDER BY 1, 2, 3")
    before = cur.fetchall()
    cur.execute("DELETE FROM mf_ter_map")                                                  # start from nothing: what a changed master or key rule would force
    r = L.rematch(conn)
    cur.execute("SELECT nsdl_code, scheme_code, plan FROM mf_ter_map ORDER BY 1, 2, 3")
    assert cur.fetchall() == before and r["mapped_rows"] == len(before)
    cur.execute("SELECT count(*) AS n FROM mf_ter_unmatched WHERE file_id = (SELECT max(id) FROM mf_ter_files)")
    assert cur.fetchone()["n"] == r["unmatched_rows"]
