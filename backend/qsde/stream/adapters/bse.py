"""BSE daily bhavcopy (official BSE archive) on the streaming foundation.

Three file formats exist; the adapter tries them in the order that fits the date and keeps the raw bytes first:
  udiff        BhavCopy_BSE_CM_0_0_0_<yyyymmdd>_F_0000.CSV   (2024-06 onward; has ISIN and TckrSymb)
  eq_isincode  EQ_ISINCODE_<ddmmyy>.zip                      (about 2017-18 .. 2024-07; has ISIN, no ticker)
  eq_legacy    EQ<ddmmyy>_CSV.ZIP                            (older; no ISIN, no ticker)
A non-session day does not return 404: BSE answers with an HTML page. That is "no session", not an error.
The BSE scrip code is the stable key (`scrip_code`); the output is NOT shaped like NSE's eod_raw.
"""
from __future__ import annotations

import io
import logging
import zipfile
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Optional

import numpy as np
import pandas as pd

from qsde.stream.adapters.nse import HttpFetcher, _ist_today
from qsde.stream.base import RawItem, SourceAdapter

log = logging.getLogger(__name__)

BASE = "https://www.bseindia.com/download/BhavCopy/Equity/"
URL_UDIFF = BASE + "BhavCopy_BSE_CM_0_0_0_{yyyymmdd}_F_0000.CSV"
URL_ISIN = BASE + "EQ_ISINCODE_{ddmmyy}.zip"
URL_LEGACY = BASE + "EQ{ddmmyy}_CSV.ZIP"
HEADERS = {"Referer": "https://www.bseindia.com/", "Accept": "*/*"}
UDIFF_FROM = date(2024, 6, 1)                      # earliest date tried for the new format (the probe found it from 2024-06-28)
IST_CLOSE_HOUR = 18

OUT_COLS = ["scrip_code", "date", "ticker", "name", "grp", "isin", "open", "high", "low", "close", "last", "prev_close", "volume",
            "turnover_value", "trades", "source_format", "source_file"]


def is_data_file(payload: bytes) -> bool:
    """A real bhavcopy is a zip (PK) or a CSV with a header; BSE's holiday answer is an HTML page."""
    head = payload[:400].lstrip()
    if head[:5].lower() in (b"<!doc", b"<html") or head[:1] == b"<":
        return False
    return head[:2] == b"PK" or b"," in head[:300]


def urls_for(d: date) -> list:
    out = []
    if d >= UDIFF_FROM:
        out.append(("udiff", URL_UDIFF.format(yyyymmdd=f"{d:%Y%m%d}")))
    out.append(("eq_isincode", URL_ISIN.format(ddmmyy=f"{d:%d%m%y}")))
    out.append(("eq_legacy", URL_LEGACY.format(ddmmyy=f"{d:%d%m%y}")))
    return out


def _num(s) -> pd.Series:
    return pd.to_numeric(s, errors="coerce")


def parse_bhavcopy(payload: bytes, session: Optional[date], source_file: str) -> pd.DataFrame:
    """Any of the three formats -> OUT_COLS (+ event_time). Untraded scrips (zero open/high/low) keep their close and get NaN O/H/L."""
    if payload[:2] == b"PK":
        z = zipfile.ZipFile(io.BytesIO(payload))
        raw = pd.read_csv(z.open(z.namelist()[0]), dtype=str, skipinitialspace=True)
    else:
        raw = pd.read_csv(io.BytesIO(payload), dtype=str, skipinitialspace=True)
    raw.columns = [c.strip() for c in raw.columns]
    if "TckrSymb" in raw.columns:                                           # UDiFF
        raw = raw[raw.get("FinInstrmTp", "STK").fillna("STK").str.strip().eq("STK") & raw.get("Sgmt", "CM").fillna("CM").str.strip().eq("CM")]
        df = pd.DataFrame({
            "scrip_code": _num(raw["FinInstrmId"]), "date": pd.to_datetime(raw["TradDt"], errors="coerce"),
            "ticker": raw["TckrSymb"].str.strip(), "name": raw["FinInstrmNm"].str.strip(), "grp": raw["SctySrs"].str.strip(),
            "isin": raw["ISIN"].str.strip(), "open": _num(raw["OpnPric"]), "high": _num(raw["HghPric"]), "low": _num(raw["LwPric"]),
            "close": _num(raw["ClsPric"]), "last": _num(raw["LastPric"]), "prev_close": _num(raw["PrvsClsgPric"]),
            "volume": _num(raw["TtlTradgVol"]), "turnover_value": _num(raw["TtlTrfVal"]), "trades": _num(raw["TtlNbOfTxsExctd"]),
            "source_format": "udiff"})
    elif "SC_CODE" in raw.columns:                                          # EQ_ISINCODE or the legacy EQ file
        has_isin = "ISIN_CODE" in raw.columns
        dt = pd.to_datetime(raw["TRADING_DATE"], format="%d-%b-%y", errors="coerce") if "TRADING_DATE" in raw.columns else pd.Series(
            pd.Timestamp(session) if session else pd.NaT, index=raw.index)
        df = pd.DataFrame({
            "scrip_code": _num(raw["SC_CODE"]), "date": dt, "ticker": None, "name": raw["SC_NAME"].str.strip(), "grp": raw["SC_GROUP"].str.strip(),
            "isin": raw["ISIN_CODE"].str.strip() if has_isin else None, "open": _num(raw["OPEN"]), "high": _num(raw["HIGH"]),
            "low": _num(raw["LOW"]), "close": _num(raw["CLOSE"]), "last": _num(raw["LAST"]), "prev_close": _num(raw["PREVCLOSE"]),
            "volume": _num(raw["NO_OF_SHRS"]), "turnover_value": _num(raw["NET_TURNOV"]), "trades": _num(raw["NO_TRADES"]),
            "source_format": "eq_isincode" if has_isin else "eq_legacy"})
    else:
        raise ValueError(f"unrecognised BSE bhavcopy header: {list(raw.columns)[:6]}")
    df["source_file"] = source_file
    for c in ("open", "high", "low"):                                       # an untraded scrip prints zeros: no trade, no OHLC
        df.loc[df[c] <= 0, c] = np.nan
    df["isin"] = df["isin"].where(df["isin"].fillna("").str.match(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$"), None)
    df = df[df["scrip_code"].notna() & df["date"].notna()].copy()
    df["scrip_code"] = df["scrip_code"].astype(int)
    df["event_time"] = [datetime.combine(d.date(), datetime.min.time().replace(hour=15, minute=30),
                                         tzinfo=timezone(timedelta(hours=5, minutes=30))).astimezone(timezone.utc) for d in df["date"]]
    return df[OUT_COLS + ["event_time"]]


class BseBhavcopyAdapter(SourceAdapter):
    name, table = "bse_bhavcopy", "bse_eod_raw"
    key_cols = ("scrip_code", "date")
    slo_seconds = 26 * 3600
    interval_seconds = 1800
    backfill_lag = timedelta(hours=2, minutes=30)
    min_rows = 1500                                     # ~2,900 scrips in 2016, ~5,000 now: a short file is a broken fetch

    def __init__(self, fetcher=None, now: Callable[[], datetime] = lambda: datetime.now(timezone.utc), start_date: Optional[date] = None,
                 max_days_per_run: int = 25, landed_sessions=(), min_rows: Optional[int] = None):
        self.fetcher = fetcher or HttpFetcher(min_interval_s=1.0)
        self.now, self.start_date, self.max_days = now, start_date, max_days_per_run
        self.landed = set(landed_sessions)
        self.no_file: set = set()
        if min_rows is not None:
            self.min_rows = min_rows

    def _candidate_dates(self, since: Optional[datetime]) -> list:
        now = self.now()
        last = _ist_today(now) if now.astimezone(timezone(timedelta(hours=5, minutes=30))).hour >= IST_CLOSE_HOUR else _ist_today(now) - timedelta(days=1)
        first = self.start_date or (since.astimezone(timezone(timedelta(hours=5, minutes=30))).date() if since else last)
        d, out = first, []
        while d <= last:
            if d.weekday() < 5 and d not in self.landed and d not in self.no_file:
                out.append(d)
            d += timedelta(days=1)
        return out[: self.max_days]

    def fetch(self, since: Optional[datetime]) -> list:
        items = []
        for d in self._candidate_dates(since):
            got = None
            for fmt, url in urls_for(d):
                try:
                    payload = self.fetcher.get(url, HEADERS)
                except Exception as e:  # noqa: BLE001 - a missing format is expected; only all-formats-missing means "no session"
                    log.info("bse %s %s: %s", d, fmt, e)
                    continue
                if is_data_file(payload):
                    got = (fmt, url, payload)
                    break
            if got is None:
                self.no_file.add(d)                         # holiday / not yet published: not an error
                continue
            fmt, url, payload = got
            self.landed.add(d)
            ext = "zip" if payload[:2] == b"PK" else "csv"
            items.append(RawItem(payload, self.now(), None, {"url": url, "session_date": str(d), "format": fmt}, ext))
        return items

    def parse(self, item: RawItem) -> pd.DataFrame:
        sess = date.fromisoformat(item.meta["session_date"]) if "session_date" in item.meta else None
        df = parse_bhavcopy(item.payload, sess, f"bse:{item.meta.get('url', 'bhavcopy')}")
        df.attrs["session"] = sess
        return df

    def validate(self, df: pd.DataFrame) -> list:
        errs = super().validate(df)
        if df.empty:
            return errs + ["empty bse bhavcopy"]
        if len(df) < self.min_rows:
            errs.append(f"only {len(df)} rows (< {self.min_rows}): truncated or wrong file")
        sess = df.attrs.get("session")
        if sess is not None and (df["date"].dt.date != sess).mean() > 0.5:
            errs.append(f"file for {sess} holds {df['date'].dt.date.mode().iloc[0]}")
        bad = ((df["close"].fillna(0) <= 0) | (df["high"] < df[["open", "close"]].max(axis=1) - 1e-6) | (df["low"] > df[["open", "close"]].min(axis=1) + 1e-6))
        if bad.sum() > max(25, 0.01 * len(df)):
            errs.append(f"{int(bad.sum())} of {len(df)} rows fail price sanity")
        return errs
