"""Where Stoxsy keeps its data on disk, in one place, for Windows and Linux.

    STOXSY_ROOT      workspace root           (default D:\\Stoxsy on Windows, /srv/stoxsy elsewhere)
    STOXSY_DATA_DIR  data directory           (default <root>/data)

Every module that needs a data path calls `data_path("landing", "nse")` instead of writing a literal, so the same code runs on the Debian host and on a Windows
machine. Individual overrides that already existed (STOXSY_LANDING_DIR, STOXSY_LEDGER_DIR, STOXSY_FEATURE_DIR, ...) keep working on top of this.
"""
from __future__ import annotations

import os
from pathlib import Path

DEFAULT_ROOT = Path(r"D:\Stoxsy") if os.name == "nt" else Path("/srv/stoxsy")
ROOT = Path(os.getenv("STOXSY_ROOT", str(DEFAULT_ROOT)))
DATA = Path(os.getenv("STOXSY_DATA_DIR", str(ROOT / "data")))


def data_path(*parts: str) -> Path:
    """A path under the data directory, e.g. data_path('reports', 'fno')."""
    return DATA.joinpath(*parts)


def root_path(*parts: str) -> Path:
    return ROOT.joinpath(*parts)
