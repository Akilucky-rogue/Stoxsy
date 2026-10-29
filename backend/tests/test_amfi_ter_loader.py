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
