"""NSE trading holidays from NSE's official holiday master, on the streaming foundation (raw first).

  https://www.nseindia.com/api/holiday-master?type=trading     JSON {segment: [{tradingDate 'dd-Mon-yyyy', weekDay, description, ...}]}; CM = cash, FO = futures and options

The endpoint needs the cookies NSE sets on its public pages, so `CookieFetcher` visits the holidays page first. It returns the CURRENT calendar year only, so history is built by landing it every
year (the daily routine runs it; an unchanged file changes nothing). A description ending in '*' is NSE's mark for a special-timing day (Muhurat trading): stored with special_session = true.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

import pandas as pd

from qsde.stream.base import RawItem, SourceAdapter

log = logging.getLogger(__name__)

PAGE = "https://www.nseindia.com/resources/exchange-communication-holidays"
URL = "https://www.nseindia.com/api/holiday-master?type=trading"
SEGMENTS = ("CM", "FO")
IST = timezone(timedelta(hours=5, minutes=30))
OUT_COLS = ["exchange", "segment", "holiday_date", "weekday", "description", "special_session"]


class CookieFetcher:
    """The only network code for this source: one requests session that picks up NSE's cookies from a public page, then reads the JSON endpoint."""

    def __init__(self, timeout_s: int = 30):
        self.timeout = timeout_s

    def get(self, url: str, headers: Optional[dict] = None) -> bytes:
        import requests

        s = requests.Session()
        s.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
                          "Accept": "application/json,text/plain,*/*", "Accept-Language": "en-US,en;q=0.9", "Referer": PAGE, **(headers or {})})
        s.get(PAGE, timeout=self.timeout)
        r = s.get(url, timeout=self.timeout)
        r.raise_for_status()
        return r.content


def parse_holidays(payload: bytes) -> pd.DataFrame:
    """Pure: JSON bytes -> one row per segment and date. Raises ValueError on anything that is not the expected document."""
    try:
        j = json.loads(payload)
    except ValueError as e:
        raise ValueError(f"not JSON (an error page?): {e}") from e
    if not isinstance(j, dict) or not all(s in j for s in SEGMENTS):
        raise ValueError(f"segments {SEGMENTS} missing from the holiday document")
    rows = []
    for seg in SEGMENTS:
        for r in j[seg]:
            d = pd.to_datetime(str(r.get("tradingDate", "")), format="%d-%b-%Y", errors="coerce")
            if pd.isna(d):
                raise ValueError(f"unparseable holiday date {r.get('tradingDate')!r} in {seg}")
            desc = str(r.get("description", "")).strip()
            rows.append({"exchange": "NSE", "segment": seg, "holiday_date": d.normalize(), "weekday": str(r.get("weekDay", "")).strip(), "description": desc.rstrip("*").strip(),
                         "special_session": desc.endswith("*")})
    df = pd.DataFrame(rows, columns=OUT_COLS)
    df["event_time"] = [datetime.combine(x.date(), datetime.min.time(), tzinfo=IST).astimezone(timezone.utc) for x in df["holiday_date"]]
    df["_sha256"] = hashlib.sha256(payload).hexdigest()
    return df


class NseHolidaysAdapter(SourceAdapter):
    name, table = "nse_holidays", "exchange_holidays"
    key_cols = ("exchange", "segment", "holiday_date")
    slo_seconds = 40 * 24 * 3600
    interval_seconds = 24 * 3600

    def __init__(self, fetcher=None, now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.fetcher = fetcher or CookieFetcher()
        self.now = now
        self._seen: set = set()

    def fetch(self, since: Optional[datetime]) -> list:
        try:
            payload = self.fetcher.get(URL)
        except Exception as e:  # noqa: BLE001
            log.warning("nse holidays: %s", e)
            return []
        sha = hashlib.sha256(payload).hexdigest()
        if sha in self._seen:
            return []
        self._seen.add(sha)
        return [RawItem(payload, self.now(), None, {"url": URL}, "json")]

    def parse(self, item: RawItem) -> pd.DataFrame:
        return parse_holidays(item.payload)

    def validate(self, df: pd.DataFrame) -> list:
        errs = super().validate(df)
        if df.empty:
            return errs + ["no holidays in the document"]
        wd = df["holiday_date"].dt.day_name()
        bad = df[wd != df["weekday"]]
        if len(bad):
            errs.append(f"weekday disagrees with the date for {sorted(bad['holiday_date'].dt.date.astype(str))[:3]}")
        years = df["holiday_date"].dt.year.unique()
        if len(years) > 2:
            errs.append(f"unexpected span of years {sorted(years.tolist())}")
        for seg in SEGMENTS:
            if (df["segment"] == seg).sum() < 8:
                errs.append(f"only {(df['segment'] == seg).sum()} holidays for {seg}: a full year has more than that")
        return errs
