"""NSE end-of-day bundle on the streaming foundation: security-wise bhavcopy, corporate actions, symbol changes.

All network access goes through `HttpFetcher` (injectable). Adapters never schedule themselves and nothing here runs on import;
scheduled downloads are enabled only by an explicit flag in scripts/stream_run.py after replay and failure tests pass.

Formats (verified against live responses on 2026-10-18):
  bhavcopy        https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_DDMMYYYY.csv
                  columns " SYMBOL, SERIES, DATE1, PREV_CLOSE, OPEN_PRICE, ... DELIV_PER" (padded with spaces); 404 on non-trading
                  days; no ISIN
  corporate acts  https://www.nseindia.com/api/corporates-corporateActions?index=equities&from_date=DD-MM-YYYY&to_date=DD-MM-YYYY
                  JSON list: symbol, series, comp, isin, subject, faceVal, exDate, recDate (dd-Mon-yyyy)
  symbol changes  https://nsearchives.nseindia.com/content/equities/symbolchange.csv
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import time
import urllib.error
import urllib.request
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Callable, Optional

import numpy as np
import pandas as pd

from qsde.stream.base import RawItem, SourceAdapter
from qsde.stream.health import IST

log = logging.getLogger(__name__)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")
BHAV_URL = "https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{ddmmyyyy}.csv"
CA_URL = ("https://www.nseindia.com/api/corporates-corporateActions?index=equities&from_date={a}&to_date={b}")
SYMCHG_URL = "https://nsearchives.nseindia.com/content/equities/symbolchange.csv"
PUBLISH_AFTER_IST = dtime(18, 30)             # bhavcopy is not final before this


class HttpError(Exception):
    def __init__(self, status: int, url: str):
        super().__init__(f"HTTP {status} for {url}")
        self.status, self.url = status, url


class HttpFetcher:
    """The only network code. Polite (minimum spacing), retried with backoff, 404 surfaced as HttpError(404)."""

    def __init__(self, min_interval_s: float = 1.5, retries: int = 3, timeout_s: int = 40,
                 sleep: Callable[[float], None] = time.sleep):
        self.min_interval, self.retries, self.timeout, self._sleep, self._last = min_interval_s, retries, timeout_s, sleep, 0.0

    def get(self, url: str, headers: Optional[dict] = None) -> bytes:
        h = {"User-Agent": UA, "Accept": "*/*", **(headers or {})}
        for attempt in range(self.retries):
            wait = self.min_interval - (time.monotonic() - self._last)
            if wait > 0:
                self._sleep(wait)
            self._last = time.monotonic()
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=self.timeout) as r:
                    return r.read()
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    raise HttpError(404, url) from e
                if e.code in (429, 500, 502, 503, 504) and attempt < self.retries - 1:
                    self._sleep(2 ** attempt * 3)
                    continue
                raise HttpError(e.code, url) from e
            except (urllib.error.URLError, TimeoutError):
                if attempt >= self.retries - 1:
                    raise
                self._sleep(2 ** attempt * 3)
        raise RuntimeError("unreachable")


def _ist_today(now: datetime) -> date:
    return now.astimezone(IST).date()


def _session_close_utc(d: date) -> datetime:
    return datetime.combine(d, dtime(15, 30), tzinfo=IST).astimezone(timezone.utc)


# ── security-wise bhavcopy ───────────────────────────────────────────

class NseBhavcopyAdapter(SourceAdapter):
    name, table = "nse_bhavcopy", "eod_raw"
    key_cols = ("exchange", "symbol", "series", "date")
    slo_seconds = 26 * 3600                    # a trading day's file must be in within ~a day of publication
    interval_seconds = 1800
    backfill_lag = timedelta(hours=2, minutes=30)   # event_time is 15:30 IST; the file is knowable from ~18:00 IST

    min_rows = 1000                              # a real session file has ~3,500 rows; a short file is a broken fetch

    def __init__(self, fetcher=None, now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 start_date: Optional[date] = None, max_days_per_run: int = 10, landed_sessions=(), min_rows: Optional[int] = None):
        self.fetcher, self.now, self.start_date, self.max_days = fetcher or HttpFetcher(), now, start_date, max_days_per_run
        self.landed = set(landed_sessions)       # sessions already in the landing zone: resumable backfill, no repeat downloads
        self.no_file: set = set()                # sessions that returned 404 in this process (holidays)
        if min_rows is not None:
            self.min_rows = min_rows

    def _candidate_dates(self, since: Optional[datetime]) -> list:
        now = self.now()
        last = _ist_today(now) if now.astimezone(IST).time() >= PUBLISH_AFTER_IST else _ist_today(now) - timedelta(days=1)
        # an explicit start date (backfill) wins over the fetch-time checkpoint, which is not a session cursor
        first = self.start_date or (since.astimezone(IST).date() if since else last)
        d, out = first, []
        while d <= last:
            if d.weekday() < 5 and d not in self.landed and d not in self.no_file:
                out.append(d)
            d += timedelta(days=1)
        return out[: self.max_days]

    def fetch(self, since: Optional[datetime]) -> list:
        items = []
        for d in self._candidate_dates(since):
            url = BHAV_URL.format(ddmmyyyy=f"{d:%d%m%Y}")
            try:
                payload = self.fetcher.get(url)
            except HttpError as e:
                if e.status == 404:                       # holiday, or not published yet: not an error
                    log.info("bhavcopy %s: 404 (non-trading day or not yet published)", d)
                    self.no_file.add(d)
                    continue
                raise
            self.landed.add(d)
            items.append(RawItem(payload, self.now(), None, {"url": url, "session_date": str(d)}, "csv"))
        return items

    def parse(self, item: RawItem) -> pd.DataFrame:
        from qsde.ingestion.nse_eod import normalize_legacy_frame

        raw = pd.read_csv(io.BytesIO(item.payload), skipinitialspace=True, dtype=str)
        raw.columns = [c.strip().upper() for c in raw.columns]
        for c in raw.columns:
            raw[c] = raw[c].str.strip()
        sess = date.fromisoformat(item.meta["session_date"]) if "session_date" in item.meta else None
        df = normalize_legacy_frame(raw, sess or pd.to_datetime(raw["DATE1"].iloc[0]).date(),
                                    f"stream:{item.meta.get('url', 'bhavcopy')}")
        if len(df) and df["date_mismatch"].all() and df["date"].nunique() == 1:
            # NSE answers a holiday URL with the PREVIOUS trading day's file (2026-05-01 returned 30-Apr data). Not a session:
            # the raw file stays landed for the record, nothing is loaded, and it is not an error.
            out = df.iloc[0:0].copy()
            out.attrs["holiday_copy"] = f"file for {sess} holds {df['date'].iloc[0]}"
            out["event_time"] = pd.Series(dtype="datetime64[ns, UTC]")
            return out
        df["event_time"] = [_session_close_utc(d) for d in df["date"]]
        return df

    def validate(self, df: pd.DataFrame) -> list:
        from qsde.ingestion.nse_eod import validate_eod

        if df.empty and df.attrs.get("holiday_copy"):
            return []
        errs = super().validate(df)
        if df.empty:
            return errs + ["empty bhavcopy"]
        if len(df) < self.min_rows:
            errs.append(f"only {len(df)} equity rows (< {self.min_rows}): truncated or wrong file")
        if "date_mismatch" in df.columns and df["date_mismatch"].any():
            # the date printed INSIDE the file must be the session we asked for (the old archive had holiday files holding the
            # previous day's data under the holiday's name)
            errs.append(f"{int(df['date_mismatch'].sum())} rows carry a date other than the requested session")
        res = validate_eod(df)
        rejected = len(df) - len(res.valid)
        if rejected > max(25, 0.01 * len(df)):           # a handful of bad rows is data; many is a format change
            errs.append(f"{rejected} of {len(df)} rows failed validation")
        return errs


# ── corporate actions (NSE API) ──────────────────────────────────────

def _parse_date(s) -> Optional[date]:
    if s is None or str(s).strip() in ("", "-"):
        return None
    t = pd.to_datetime(str(s).strip(), format="%d-%b-%Y", errors="coerce")
    return None if pd.isna(t) else t.date()


class NseCorporateActionsAdapter(SourceAdapter):
    name, table = "nse_corporate_actions", "nse_corporate_actions"
    key_cols = ("symbol", "ex_date", "subject", "record_date", "series")
    slo_seconds = 36 * 3600
    interval_seconds = 3600
    backfill_lag = timedelta(hours=0)

    def __init__(self, fetcher=None, now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 lookback_days: int = 45, lookahead_days: int = 60, windows: Optional[list] = None, landed_windows=()):
        self.fetcher, self.now, self.lookback, self.lookahead = fetcher or HttpFetcher(), now, lookback_days, lookahead_days
        self.windows = windows                       # [(from_date, to_date)] for a history backfill; None = rolling live window
        self.landed_windows = set(landed_windows)    # windows already in the landing zone (resumable)

    @staticmethod
    def quarter_windows(start: date, end: date, days: int = 92) -> list:
        out, d = [], start
        while d <= end:
            e = min(d + timedelta(days=days - 1), end)
            out.append((d, e))
            d = e + timedelta(days=1)
        return out

    def fetch(self, since: Optional[datetime]) -> list:
        if self.windows is not None:
            items = []
            for a, b in self.windows:
                if (a, b) in self.landed_windows:
                    continue
                url = CA_URL.format(a=f"{a:%d-%m-%Y}", b=f"{b:%d-%m-%Y}")
                payload = self.fetcher.get(url, {"Accept": "application/json", "Referer":
                                                 "https://www.nseindia.com/companies-listing/corporate-filings-actions"})
                self.landed_windows.add((a, b))
                items.append(RawItem(payload, self.now(), None, {"url": url, "from": str(a), "to": str(b), "backfill": True}, "json"))
                if len(items) >= 8:                  # a few windows per run; the script loops until none are left
                    break
            return items
        today = _ist_today(self.now())
        a = (today - timedelta(days=self.lookback))
        b = today + timedelta(days=self.lookahead)
        url = CA_URL.format(a=f"{a:%d-%m-%Y}", b=f"{b:%d-%m-%Y}")
        payload = self.fetcher.get(url, {"Accept": "application/json", "Referer":
                                         "https://www.nseindia.com/companies-listing/corporate-filings-actions"})
        return [RawItem(payload, self.now(), None, {"url": url, "from": str(a), "to": str(b)}, "json")]

    def parse(self, item: RawItem) -> pd.DataFrame:
        from qsde.ingestion.corp_actions_source import parse_purpose

        rows = json.loads(item.payload.decode("utf-8"))
        if not isinstance(rows, list):
            raise ValueError("unexpected corporate-actions payload (not a list)")
        recs = []
        for r in rows:
            subj = (r.get("subject") or "").strip()
            ex = _parse_date(r.get("exDate"))
            if not subj or ex is None:
                continue
            p = parse_purpose(subj)
            low = subj.lower()
            cat = (p["category"] if p["category"] != "OTHER" else
                   "DIVIDEND" if "dividend" in low or "div" in low.split()[:2] else
                   "RIGHTS" if "right" in low else "OTHER")
            try:
                fv = float(r.get("faceVal"))
            except (TypeError, ValueError):
                fv = np.nan
            recs.append({
                "symbol": (r.get("symbol") or "").strip(), "series": (r.get("series") or "").strip() or None,
                "isin": (r.get("isin") or "").strip() or None, "company": (r.get("comp") or "").strip(), "subject": subj,
                "category": cat, "factor": p["factor"], "face_value": fv, "ex_date": ex,
                "record_date": _parse_date(r.get("recDate")),
                "event_time": datetime.combine(ex, dtime(0, 0), tzinfo=IST).astimezone(timezone.utc),
                "source_sha256": hashlib.sha256(item.payload).hexdigest()})
        out = pd.DataFrame(recs)
        if len(out):
            # NSE history lists some actions twice as exact duplicate rows: one fact, one row
            out = out.drop_duplicates(subset=[c for c in out.columns if c != "source_sha256"]).reset_index(drop=True)
        return out

    def validate(self, df: pd.DataFrame) -> list:
        errs = super().validate(df) if len(df) else []
        if df.empty:
            return []                                    # a quiet window is legitimate
        if (df["symbol"] == "").any():
            errs.append("rows without a symbol")
        return errs


# ── symbol changes (CSV) ─────────────────────────────────────────────

class NseSymbolChangeAdapter(SourceAdapter):
    name, table = "nse_symbol_changes", "symbol_changes"
    key_cols = ("old_symbol", "new_symbol", "effective_date")
    slo_seconds = 8 * 24 * 3600
    interval_seconds = 6 * 3600
    backfill_lag = timedelta(hours=0)

    def __init__(self, fetcher=None, now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.fetcher, self.now = fetcher or HttpFetcher(), now

    def fetch(self, since: Optional[datetime]) -> list:
        return [RawItem(self.fetcher.get(SYMCHG_URL), self.now(), None, {"url": SYMCHG_URL}, "csv")]

    def parse(self, item: RawItem) -> pd.DataFrame:
        from importlib import import_module
        import sys
        from pathlib import Path
        import tempfile

        sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
        loader = import_module("load_nse_symbol_changes")
        with tempfile.NamedTemporaryFile("wb", suffix=".csv", delete=False) as f:
            f.write(item.payload)
        try:
            df, _ = loader.parse(Path(f.name))
        finally:
            Path(f.name).unlink(missing_ok=True)
        df = df.rename(columns={"name": "company"})
        df["effective_date"] = pd.to_datetime(df["effective_date"]).dt.date
        df["event_time"] = [datetime.combine(d, dtime(0, 0), tzinfo=IST).astimezone(timezone.utc) for d in df["effective_date"]]
        return df

    def validate(self, df: pd.DataFrame) -> list:
        errs = super().validate(df)
        if len(df) < 200:                                 # the real list has > 1,000 rows; a short file is a broken fetch
            errs.append(f"only {len(df)} symbol-change rows")
        return errs
