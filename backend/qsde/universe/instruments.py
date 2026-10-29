"""Non-equity instruments (ETFs, liquid-fund units) that trade in the NSE EQ series and must not be treated as stocks.

The liquidity-ranked universe (`adv60_top500_v1`) is built from traded value alone, so it contained liquid funds and gold/silver/index ETFs.
They are near-riskless or commodity-like, which silently turned them into the 'most defensive' names and into the low-volatility end of any
cross-sectional ranking.

Two reviewed, versioned lists (infra/data):
  non_equity_official_etf_v1.json   every ETF on NSE's official list (ISIN INF*), landed raw and hashed  <- the authority
  non_equity_instruments.json       the earlier name + behaviour list; items that are not on today's official file (delisted / merged ETFs)
                                    stay excluded and are labelled `not_on_current_official_list`
A symbol is excluded if it is on either list. Operating companies with GOLD / SILVER / GILT in the name are not on either and stay in.
Every dataset and signal that drops them records both list versions and fingerprints.
"""
from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path

DATA = Path(__file__).resolve().parents[3] / "infra" / "data"
PROVISIONAL = DATA / "non_equity_instruments.json"
OFFICIAL = DATA / "non_equity_official_etf_v1.json"


@lru_cache(maxsize=1)
def _provisional() -> dict:
    return json.loads(PROVISIONAL.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def _official() -> dict:
    return json.loads(OFFICIAL.read_text(encoding="utf-8")) if OFFICIAL.exists() else {"version": "none", "instruments": []}


def official_symbols() -> frozenset:
    return frozenset(i["symbol"].upper() for i in _official()["instruments"])


def provisional_symbols() -> frozenset:
    return frozenset(i["symbol"].upper() for i in _provisional()["instruments"])


def non_equity_symbols() -> frozenset:
    return official_symbols() | provisional_symbols()


def _fp(symbols) -> str:
    return hashlib.sha256(json.dumps(sorted(symbols)).encode()).hexdigest()[:10]


def version() -> str:
    return _provisional()["version"]


def fingerprint() -> str:
    return _fp(non_equity_symbols())


def tag() -> str:
    """Suffix recorded in a universe_version string: both list versions with fingerprints (provisional + NSE official)."""
    return f"ex_{_provisional()['version']}:{_fp(provisional_symbols())}+{_official()['version']}:{_fp(official_symbols())}"
