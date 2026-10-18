"""Non-equity instruments (ETFs, liquid-fund units) that trade in the NSE EQ series and must not be treated as stocks.

The liquidity-ranked universe (`adv60_top500_v1`) is built from traded value alone, so it contained liquid funds and gold/silver/index ETFs.
They are near-riskless or commodity-like, which silently turned them into the 'most defensive' names and into the low-volatility end of any
cross-sectional ranking. The reviewed list lives in infra/data/non_equity_instruments.json (versioned); every dataset and signal that drops
them records the list version and fingerprint.
"""
from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path

FILE = Path(__file__).resolve().parents[3] / "infra" / "data" / "non_equity_instruments.json"


@lru_cache(maxsize=1)
def _load() -> dict:
    return json.loads(FILE.read_text(encoding="utf-8"))


def non_equity_symbols() -> frozenset:
    return frozenset(i["symbol"].upper() for i in _load()["instruments"])


def version() -> str:
    return _load()["version"]


def fingerprint() -> str:
    return hashlib.sha256(json.dumps(sorted(non_equity_symbols())).encode()).hexdigest()[:10]


def tag() -> str:
    """Suffix recorded in a universe_version string so a card shows the equity-only filter that was applied."""
    return f"ex_{version()}:{fingerprint()}"
