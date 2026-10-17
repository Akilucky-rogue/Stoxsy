"""
ATM implied-volatility history store (F&O increment 4).

The FnoIncomeEngine's vol-rich gate is iv_rank — today's ATM IV located inside
its own trailing range. That range lives in `fno_iv_history` (migration 016).
This module is the only sanctioned reader/writer:

  * `record_atm_iv(...)`        — idempotent upsert of one session's ATM IV.
  * `load_atm_iv_history(...)`  — the trailing window for iv_rank, LOOKAHEAD-SAFE
                                  (rows STRICTLY BEFORE `before_date`).

Keeping the "< before_date" predicate here (not in the caller) means no engine
or backtest path can accidentally rank today's IV against a window that already
contains today.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Optional

from qsde.db.connection import execute_sql, read_sql

log = logging.getLogger(__name__)

_UPSERT_SQL = """
INSERT INTO fno_iv_history
    (underlying, as_of_date, expiry, spot, atm_strike, atm_iv, t_years, source)
VALUES
    (%(underlying)s, %(as_of_date)s, %(expiry)s, %(spot)s, %(atm_strike)s,
     %(atm_iv)s, %(t_years)s, %(source)s)
ON CONFLICT (underlying, as_of_date, expiry) DO UPDATE SET
    spot       = EXCLUDED.spot,
    atm_strike = EXCLUDED.atm_strike,
    atm_iv     = EXCLUDED.atm_iv,
    t_years    = EXCLUDED.t_years,
    source     = EXCLUDED.source,
    fetched_at = NOW()
"""


def record_atm_iv(
    underlying: str,
    as_of_date: date,
    expiry: date,
    atm_iv: float,
    *,
    spot: Optional[float] = None,
    atm_strike: Optional[float] = None,
    t_years: Optional[float] = None,
    source: str = "bhavcopy",
) -> bool:
    """Persist one session's ATM IV (idempotent per (underlying, day, expiry)).

    A re-run of the snapshotter the same day overwrites rather than duplicates —
    the same integrity rule as the idea-log. Returns True if a usable IV was
    written, False if atm_iv was non-finite (skipped)."""
    if atm_iv is None or atm_iv != atm_iv or atm_iv <= 0:   # None / NaN / non-positive
        log.warning("record_atm_iv: skipping non-finite IV for %s %s", underlying, as_of_date)
        return False
    execute_sql(_UPSERT_SQL, {
        "underlying": underlying.upper(),
        "as_of_date": as_of_date,
        "expiry": expiry,
        "spot": spot,
        "atm_strike": atm_strike,
        "atm_iv": float(atm_iv),
        "t_years": t_years,
        "source": source,
    })
    return True


def load_atm_iv_history(
    underlying: str,
    before_date: date,
    *,
    lookback_days: int = 252,
) -> list[float]:
    """Trailing ATM-IV values for `underlying`, STRICTLY BEFORE `before_date`.

    Lookahead-safe by construction: the day being ranked is never in its own
    window. Returns the most-recent up-to-`lookback_days` sessions' ATM IVs
    (one per session — if multiple expiries exist on a day we take the nearest,
    i.e. smallest expiry, as the front-month IV). Empty if no history yet."""
    df = read_sql(
        """
        SELECT atm_iv
          FROM (
            SELECT DISTINCT ON (as_of_date) as_of_date, atm_iv
              FROM fno_iv_history
             WHERE underlying = :u
               AND as_of_date < :d
               AND atm_iv IS NOT NULL
          ORDER BY as_of_date, expiry          -- nearest expiry = front-month IV
          ) s
      ORDER BY as_of_date DESC
         LIMIT :n
        """,
        params={"u": underlying.upper(), "d": before_date, "n": int(lookback_days)},
    )
    return [float(x) for x in df["atm_iv"].tolist()] if not df.empty else []


__all__ = ["record_atm_iv", "load_atm_iv_history"]
