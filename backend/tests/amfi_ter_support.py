"""Helpers for the AMFI TER tests. They read the owner's REAL downloaded files from the inbox; nothing here fabricates data.

`independent_frame` reads the workbook with plain pandas, without the code under test, so counts and values can be recounted independently."""
from __future__ import annotations

import time
from pathlib import Path

import pandas as pd
import pytest

from qsde.config.paths import data_path

INBOX = data_path("inbox", "amfi_ter")
IGNORED_SUFFIXES = (".crdownload", ".part", ".tmp")
SKIP_MESSAGE = "no real AMFI TER file in the inbox"


def real_files(inbox: Path = INBOX) -> list[Path]:
    """Real .xlsx/.csv files in the inbox, ignoring Excel lock files, partial downloads and files modified in the last 60 seconds."""
    if not inbox.exists():
        return []
    now = time.time()
    out = []
    for p in sorted(inbox.iterdir()):
        if not p.is_file() or p.name.startswith("~$") or p.suffix.lower() in IGNORED_SUFFIXES or p.suffix.lower() not in (".xlsx", ".csv"):
            continue
        if now - p.stat().st_mtime < 60:
            continue
        out.append(p)
    return out


def require_real_file(suffix: str = ".xlsx") -> Path:
    """The first real file with this suffix, or skip the test with a clear message."""
    files = [p for p in real_files() if p.suffix.lower() == suffix]
    if not files:
        pytest.skip(SKIP_MESSAGE)
    return files[0]


def independent_frame(path: Path) -> pd.DataFrame:
    """The workbook as text, cut at the first completely blank row (the footer starts after it). Columns keep AMFI's own headers."""
    df = pd.read_excel(path, sheet_name=0, dtype=str, engine="openpyxl")
    blank = df.isna().all(axis=1)
    cut = int(blank.idxmax()) if blank.any() else len(df)
    return df.iloc[:cut].reset_index(drop=True)
