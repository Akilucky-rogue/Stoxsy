"""Source-adapter contract for continuous data (market, filings, news, macro, ...). See docs/STREAMING_DATA.md.

An adapter does exactly three things and owns no persistence:

    fetch(since)  -> list[RawItem]        the ONLY method that touches the network
    parse(raw)    -> DataFrame             pure: bytes in, rows out; must be deterministic
    validate(df)  -> list[str]             schema / range / duplicate problems (empty = ok)

Everything else (raw-first landing, checkpoints, idempotent writes, health, alerts, replay) is done by the runner, so that
every source gets the same guarantees and a parser fix can be replayed from landed bytes without refetching.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import pandas as pd


@dataclass(frozen=True)
class RawItem:
    """Bytes exactly as received, plus how/when they were obtained."""
    payload: bytes
    fetched_at: datetime                      # tz-aware UTC; when WE received it
    published_at: Optional[datetime] = None   # publisher timestamp if the source gives one (tz-aware UTC)
    meta: dict = field(default_factory=dict)  # url, http status, etag, request params ... (JSON-serialisable)
    ext: str = "bin"


class SourceAdapter(ABC):
    name: str = "unnamed"                     # unique, filesystem-safe
    table: str = ""                           # destination table (informational)
    key_cols: tuple = ()                      # natural key -> idempotent upsert
    slo_seconds: int = 3600                   # max acceptable age of the newest data while the source should be live
    market_hours_only: bool = False           # staleness is only judged while the exchange is open
    interval_seconds: int = 300               # polling cadence used by the scheduler

    @abstractmethod
    def fetch(self, since: Optional[datetime]) -> list[RawItem]:
        """Network access lives here and nowhere else."""

    @abstractmethod
    def parse(self, item: RawItem) -> pd.DataFrame:
        """Pure and deterministic. Must include `event_time` (tz-aware UTC); `known_at` is added by the runner."""

    def validate(self, df: pd.DataFrame) -> list[str]:
        errs = []
        if "event_time" not in df.columns:
            errs.append("missing event_time")
        if self.key_cols and df.duplicated(list(self.key_cols)).any():
            errs.append("duplicate natural keys in one batch")
        return errs
