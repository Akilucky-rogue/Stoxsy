"""AMFI TER reader, tested only against the owner's real downloaded file (see docs/design/AMFI_TER_DESIGN.md). Skips when no real file is in the inbox."""
import random
import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from amfi_ter_support import INBOX, independent_frame, require_real_file  # noqa: E402
from qsde.config.paths import data_path  # noqa: E402
from qsde.ingestion import amfi_ter as T  # noqa: E402

pytestmark = pytest.mark.real_data
OCTOBER_SHA16 = "0C1DD64F0A4EB231"
SRC_COLS_REG = ["Regular Plan - Base Expense Ratio (BER) (%)", "Regular Plan - Brokerage cost (%)",
                "Regular Plan - Transaction Cost incurred for the purpose of execution of trade (%)",
                "Regular Plan - Statutory Levies (including GST) (%)", "Regular Plan - Total TER (%)"]
SRC_COLS_DIR = [c.replace("Regular Plan", "Direct Plan") for c in SRC_COLS_REG]
NUM_COLS = ["r_ber", "r_brokerage", "r_transaction", "r_statutory", "r_total", "d_ber", "d_brokerage", "d_transaction", "d_statutory", "d_total"]


@pytest.fixture(scope="module")
def real():
    f = require_real_file()
    return f, T.read_export(f.read_bytes(), f.name)


def _indep(f):
    d = independent_frame(f)
    d["nsdl_code_raw"] = d["NSDL Scheme Code"]
    d["ter_date"] = pd.to_datetime(d["TER Date"])
    return d


def test_every_source_column_is_mapped(real):
    _, ex = real
    assert ex.unmatched_headers == []
    assert len(ex.mapping) == 15


def test_rows_reconcile_and_footer_is_excluded(real):
    f, ex = real
    assert ex.n_data_rows == ex.n_parsed + ex.n_rejected
    assert ex.n_data_rows == len(independent_frame(f))
    assert ex.n_footer_rows > 0
    assert ex.footer_head.startswith("Disclaimer")
    assert not ex.frame["nsdl_code"].str.startswith("Disclaimer").any()


def test_codes_are_normalised(real):
    f, ex = real
    assert not ex.frame["nsdl_code"].str.contains(r"\s|\?", regex=True).any()
    raw = independent_frame(f)["NSDL Scheme Code"].str.replace(r"\s+", "", regex=True).str.replace(r"\?$", "", regex=True)
    assert ex.frame["nsdl_code"].nunique() == raw.nunique()


def test_all_zero_plan_is_stored_missing(real):
    f, ex = real
    d = _indep(f)
    merged = d.merge(ex.frame, on=["nsdl_code_raw", "ter_date"], how="inner")
    assert len(merged) == len(d)
    for src, prefix in ((SRC_COLS_REG, "r"), (SRC_COLS_DIR, "d")):
        zero = merged[src].apply(pd.to_numeric).eq(0).all(axis=1)
        assert zero.any(), f"the real file has no all-zero {prefix} plan rows to check"
        assert merged.loc[zero, f"{prefix}_total"].isna().all() and merged.loc[zero, f"{prefix}_ber"].isna().all()
    assert not (ex.frame["r_total"] == 0).any() and not (ex.frame["d_total"] == 0).any()


def test_values_are_unchanged(real):
    f, ex = real
    d = _indep(f)
    merged = d.merge(ex.frame, on=["nsdl_code_raw", "ter_date"], how="inner")
    rng = random.Random(7)
    for i in rng.sample(range(len(merged)), 200):
        row = merged.iloc[i]
        for src, dst in zip(SRC_COLS_REG + SRC_COLS_DIR, NUM_COLS):
            v = float(row[src])
            got = row[dst]
            plan_zero = all(float(row[c]) == 0 for c in (SRC_COLS_REG if dst.startswith("r") else SRC_COLS_DIR))
            if plan_zero:
                assert pd.isna(got)
            else:
                assert got == pytest.approx(v)


def test_october_file_facts(real):
    _, ex = real
    if ex.sha256[:16].upper() != OCTOBER_SHA16:
        pytest.skip("not the October 2026 file")
    assert ex.n_data_rows == 58095
    assert ex.frame["nsdl_code"].nunique() == 2173
    assert ex.frame["ter_date"].nunique() == 29
    assert ex.n_rejected == 0


def test_wrong_layout_real_file_is_refused():
    cands = sorted(data_path("landing", "bse_corporate_actions").rglob("*.csv")) + sorted(p for p in INBOX.glob("*.csv") if "ter" not in p.stem.lower())
    if not cands:
        pytest.skip("no real non-TER file available to test the refusal")
    with pytest.raises(ValueError) as e:
        T.read_export(cands[0].read_bytes(), cands[0].name)
    assert "NSDL" in str(e.value) or "TER column" in str(e.value)


def test_csv_resaved_from_excel_gives_same_rows(real):
    f, ex = real
    csv = f.with_suffix(".csv")
    if not csv.exists():
        pytest.skip("ask the owner to Save As CSV")
    cx = T.read_export(csv.read_bytes(), csv.name)
    assert cx.n_parsed == ex.n_parsed
    a = ex.frame[T.TER_COLS].reset_index(drop=True)
    b = cx.frame[T.TER_COLS].reset_index(drop=True)
    a["ter_date"], b["ter_date"] = pd.to_datetime(a["ter_date"]).dt.date, pd.to_datetime(b["ter_date"]).dt.date
    pd.testing.assert_frame_equal(a, b, check_dtype=False)


# ---- Task 2: row flags and partial days -------------------------------------------------------------------------------------------------

def _merged(f, ex):
    d = _indep(f)
    return d.merge(ex.frame, on=["nsdl_code_raw", "ter_date"], how="inner")


def test_flags_match_an_independent_recount(real):
    f, ex = real
    flags = T.flag_rows(ex.frame)
    assert len(flags) == len(ex.frame) and list(flags.index) == list(ex.frame.index)
    got = {k: int(flags.map(lambda x, k=k: k in x).sum()) for k in T.FLAGS}
    d = _indep(f)
    num = {c: pd.to_numeric(d[c]) for c in SRC_COLS_REG + SRC_COLS_DIR}
    r_total, d_total = num[SRC_COLS_REG[4]], num[SRC_COLS_DIR[4]]
    r_off = ~pd.concat([num[c] for c in SRC_COLS_REG], axis=1).eq(0).all(axis=1)          # plan offered = not all five values zero
    d_off = ~pd.concat([num[c] for c in SRC_COLS_DIR], axis=1).eq(0).all(axis=1)
    r_diff = (r_total - sum(num[c] for c in SRC_COLS_REG[:4])).abs()
    d_diff = (d_total - sum(num[c] for c in SRC_COLS_DIR[:4])).abs()
    want = {"total_gt_3_regular": int((r_off & (r_total > 3.0)).sum()),
            "total_gt_3_direct": int((d_off & (d_total > 3.0)).sum()),
            "direct_not_below_regular": int((r_off & d_off & (d_total >= r_total)).sum()),
            "sum_mismatch": int(((r_off & (r_diff > 0.02)) | (d_off & (d_diff > 0.02))).sum())}
    assert got == want
    assert sum(want.values()) > 0, "the real file has no flagged rows, so this recount proves nothing"


def test_flagged_values_are_not_modified(real):
    f, ex = real
    before = ex.frame.copy()
    flags = T.flag_rows(ex.frame)
    pd.testing.assert_frame_equal(before, ex.frame)                                   # flag_rows does not touch the frame
    flagged = ex.frame[flags.map(len) > 0]
    assert len(flagged) > 0
    d = _indep(f)
    m = d.merge(flagged, on=["nsdl_code_raw", "ter_date"], how="inner")
    assert len(m) == len(flagged)
    for src, dst in zip(SRC_COLS_REG + SRC_COLS_DIR, NUM_COLS):
        keep = m[dst].notna()
        assert (m.loc[keep, dst].astype(float) - m.loc[keep, src].astype(float)).abs().max() < 1e-9


def test_partial_last_day_is_detected(real):
    _, ex = real
    if ex.sha256[:16].upper() != OCTOBER_SHA16:
        pytest.skip("not the October 2026 file")
    assert T.partial_dates(ex.frame) == [date(2026, 10, 29)]


def test_the_first_five_dates_are_never_partial(real):
    _, ex = real
    first5 = sorted(ex.frame["ter_date"].dt.date.unique())[:5]
    assert not set(T.partial_dates(ex.frame)) & set(first5)
