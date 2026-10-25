"""Where Stoxsy keeps its data on disk, in one place, for Windows and Linux.

    STOXSY_ROOT      workspace root           (see `resolve_root`)
    STOXSY_DATA_DIR  data directory           (default <root>/data)

The root is the first of: the STOXSY_ROOT environment variable; STOXSY_ROOT in the repository's `.env`; the folder above this checkout when it already holds a `data` directory
(the layout is <root>/stoxsy + <root>/data); C:\\Stoxsy on Windows or /srv/stoxsy elsewhere. A new terminal that never set the variable therefore lands on the same disk as the code, never on a
stale default. (The old default was D:\\Stoxsy, the USB disk that failed; nothing may write there by accident.)

Every module that needs a data path calls `data_path("landing", "nse")` instead of writing a literal, so the same code runs on the Debian host and on a Windows
machine. Individual overrides that already existed (STOXSY_LANDING_DIR, STOXSY_LEDGER_DIR, STOXSY_FEATURE_DIR, ...) keep working on top of this.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping, Optional

REPO = Path(__file__).resolve().parents[3]                                           # <root>/stoxsy
DEFAULT_ROOT = Path(r"C:\Stoxsy") if os.name == "nt" else Path("/srv/stoxsy")


def _from_dotenv(env_file: Path) -> Optional[str]:
    """STOXSY_ROOT from a .env file; blank, commented and unrelated lines are ignored. Nothing else in the file is read."""
    try:
        for line in env_file.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if s.startswith("STOXSY_ROOT") and "=" in s:
                v = s.split("=", 1)[1].strip().strip('"').strip("'")
                if v:
                    return v
    except OSError:
        pass
    return None


def resolve_root(environ: Optional[Mapping] = None, repo: Optional[Path] = None, default: Optional[Path] = None) -> Path:
    environ = os.environ if environ is None else environ
    repo = REPO if repo is None else repo
    if environ.get("STOXSY_ROOT"):
        return Path(environ["STOXSY_ROOT"])
    v = _from_dotenv(repo / ".env")
    if v:
        return Path(v)
    if (repo.parent / "data").is_dir():
        return repo.parent
    return DEFAULT_ROOT if default is None else default


ROOT = resolve_root()
DATA = Path(os.getenv("STOXSY_DATA_DIR", str(ROOT / "data")))


def data_path(*parts: str) -> Path:
    """A path under the data directory, e.g. data_path('reports', 'fno')."""
    return DATA.joinpath(*parts)


def root_path(*parts: str) -> Path:
    return ROOT.joinpath(*parts)
