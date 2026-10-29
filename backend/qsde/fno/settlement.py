"""
Index settlement source for condor resolution (F&O increment 5b).

`resolve_option_ideas` needs the underlying's settlement price on expiry to score a
condor. This composes the available sources, cheapest first, and NEVER guesses —
returns None when none can answer (the resolver then waits):

  1. **Persisted spot** — the index level we already recorded in `fno_iv_history`
     on the expiry session (free, no I/O beyond the DB).
  2. **Expiry-day F&O bhavcopy `UndrlygPric`** — the SAME free EOD file the chain
     source already parses carries the underlying price per row; on expiry day that
     is the settlement. Needs NSE access but no Kite token.
  3. **Injected extras** — e.g. a Kite index historical-close lookup for live use,
     passed in so this module stays import-light and testable.

Every source is injectable, so the composition is unit-tested with no DB / network.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Callable, Iterable, Optional

from qsde.fno.bhavcopy import extract_underlying_spot, fetch_fo_bhavcopy, parse_fo_bhavcopy

log = logging.getLogger(__name__)

Lookup = Callable[[str, date], Optional[float]]


def settlement_from_bhavcopy(text: str, underlying: str, expiry: date) -> Optional[float]:
    """PURE: the index settlement for (underlying, expiry) from an F&O bhavcopy's
    `UndrlygPric`. Tries the exact expiry's rows first, then any of the underlying's
    rows (the index level is the same across expiries on a day). None if absent."""
    df = parse_fo_bhavcopy(text)
    if df.empty:
        return None
    s = extract_underlying_spot(df, underlying, expiry)
    if not (s == s):                                   # NaN -> try any expiry
        s = extract_underlying_spot(df, underlying, None)
    return float(s) if s == s else None


def persisted_index_spot(underlying: str, expiry: date) -> Optional[float]:
    """The index level recorded on the expiry session (fno_iv_history.spot), else the
    equity-ohlcv close as a fallback. DB-backed; None if neither exists."""
    from qsde.db.connection import read_sql
    iv = read_sql(
        "SELECT spot FROM fno_iv_history WHERE underlying = :u AND as_of_date = :d "
        "AND spot IS NOT NULL ORDER BY expiry LIMIT 1",
        params={"u": str(underlying).upper(), "d": expiry},
    )
    if not iv.empty and iv.iloc[0]["spot"] is not None:
        return float(iv.iloc[0]["spot"])
    px = read_sql(
        "SELECT close FROM ohlcv WHERE symbol = :s AND date = :d LIMIT 1",
        params={"s": str(underlying).upper(), "d": expiry},
    )
    if not px.empty and px.iloc[0]["close"] is not None:
        return float(px.iloc[0]["close"])
    return None


def index_settlement(
    underlying: str, expiry: date, *,
    persisted: Optional[Lookup] = None,
    bhavcopy_fetch: Optional[Callable[[date], str]] = None,
    fetch: bool = True,
    extra_lookups: Iterable[Lookup] = (),
) -> Optional[float]:
    """Underlying settlement on `expiry`, composing sources cheapest-first. Returns
    None when none can answer (the resolver then keeps the idea OPEN — never guesses).
    All sources injectable for testing."""
    persisted = persisted or persisted_index_spot
    bhavcopy_fetch = bhavcopy_fetch or fetch_fo_bhavcopy

    s = persisted(underlying, expiry)
    if s is not None:
        return float(s)

    if fetch:
        try:
            s = settlement_from_bhavcopy(bhavcopy_fetch(expiry), underlying, expiry)
            if s is not None:
                return float(s)
        except Exception as e:  # noqa: BLE001 — NSE blocks bots; just fall through
            log.warning("index_settlement: bhavcopy fetch failed for %s %s: %s",
                        underlying, expiry, e)

    for fn in extra_lookups:
        try:
            s = fn(underlying, expiry)
            if s is not None:
                return float(s)
        except Exception as e:  # noqa: BLE001
            log.warning("index_settlement: extra lookup failed: %s", e)
    return None


__all__ = ["settlement_from_bhavcopy", "persisted_index_spot", "index_settlement", "Lookup"]
