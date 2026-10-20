"""NSE derivatives (F&O) end-of-day bhavcopy from NSE's official archive, on the streaming foundation. Raw files are landed first.

  legacy  https://nsearchives.nseindia.com/content/historical/DERIVATIVES/<YYYY>/<MON>/fo<DD><MON><YYYY>bhav.csv.zip   (through 2024-07-05)
  udiff   https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_<yyyymmdd>_F_0000.csv.zip                  (2024-06 onward; the only format after 2024-07-05)
A date with no file (holiday, weekend, not yet published) answers 404 / an error page: that is "no session", not an error. Every listed contract of every strike is in the file; rows with
neither open interest nor volume are not stored (counted in the session registry instead). The two formats overlap for a week in mid-2024, which allows a cross-check.
"""
from __future__ import annotations

import hashlib
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

URL_LEGACY = "https://nsearchives.nseindia.com/content/historical/DERIVATIVES/{yyyy}/{mon}/fo{dd}{mon}{yyyy}bhav.csv.zip"
URL_UDIFF = "https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{yyyymmdd}_F_0000.csv.zip"
HEADERS = {"Referer": "https://www.nseindia.com/", "Accept": "*/*"}
LEGACY_LAST = date(2024, 7, 5)
UDIFF_FIRST = date(2024, 6, 28)
IST_CLOSE_HOUR = 19
UDIFF_TYPES = {"IDF": "FUTIDX", "STF": "FUTSTK", "IDO": "OPTIDX", "STO": "OPTSTK"}
OUT_COLS = ["trade_date", "instrument", "symbol", "expiry_date", "strike", "option_type", "open", "high", "low", "close", "settle", "last", "prev_close", "underlying_price",
            "contracts", "value_inr", "open_interest", "chg_oi", "trades", "lot_size", "fin_instrm_id", "source_format", "source_file"]


def is_data_file(payload: bytes) -> bool:
    return payload[:2] == b"PK"


def urls_for(d: date) -> list:
    mon = f"{d:%b}".upper()
    leg = ("legacy", URL_LEGACY.format(yyyy=f"{d:%Y}", mon=mon, dd=f"{d:%d}"))
    udi = ("udiff", URL_UDIFF.format(yyyymmdd=f"{d:%Y%m%d}"))
    if d > LEGACY_LAST:
        return [udi]
    if d < UDIFF_FIRST:
        return [leg]
    return [leg, udi]


def _csv(payload: bytes) -> pd.DataFrame:
    with zipfile.ZipFile(io.BytesIO(payload)) as z:
        name = next(n for n in z.namelist() if n.lower().endswith(".csv"))
        return pd.read_csv(z.open(name), dtype=str, skipinitialspace=True, index_col=False)


def _n(s) -> pd.Series:
    return pd.to_numeric(s, errors="coerce")


def parse_fo(payload: bytes, fmt: str, source_file: str) -> pd.DataFrame:
    """Either format -> OUT_COLS (+ event_time). Idle contracts (no OI, no volume) are dropped and counted; other instrument types are counted and dropped."""
    raw = _csv(payload)
    raw.columns = [c.strip() for c in raw.columns]
    n_file = len(raw)
    if fmt == "legacy":
        d = pd.DataFrame({"trade_date": pd.to_datetime(raw["TIMESTAMP"], format="%d-%b-%Y", errors="coerce"), "instrument": raw["INSTRUMENT"].str.strip(),
                          "symbol": raw["SYMBOL"].str.strip(), "expiry_date": pd.to_datetime(raw["EXPIRY_DT"], format="%d-%b-%Y", errors="coerce"),
                          "strike": _n(raw["STRIKE_PR"]).fillna(0.0), "option_type": raw["OPTION_TYP"].str.strip().replace({"XX": "FUT"}),
                          "open": _n(raw["OPEN"]), "high": _n(raw["HIGH"]), "low": _n(raw["LOW"]), "close": _n(raw["CLOSE"]), "settle": _n(raw["SETTLE_PR"]),
                          "last": np.nan, "prev_close": np.nan, "underlying_price": np.nan, "contracts": _n(raw["CONTRACTS"]), "value_inr": _n(raw["VAL_INLAKH"]) * 1e5,
                          "open_interest": _n(raw["OPEN_INT"]), "chg_oi": _n(raw["CHG_IN_OI"]), "trades": np.nan, "lot_size": np.nan, "fin_instrm_id": np.nan})
    elif fmt == "udiff":
        tp = raw["FinInstrmTp"].str.strip()
        d = pd.DataFrame({"trade_date": pd.to_datetime(raw["TradDt"], format="%Y-%m-%d", errors="coerce"), "instrument": tp.map(UDIFF_TYPES), "symbol": raw["TckrSymb"].str.strip(),
                          "expiry_date": pd.to_datetime(raw["XpryDt"], format="%Y-%m-%d", errors="coerce"), "strike": _n(raw["StrkPric"]).fillna(0.0),
                          "option_type": raw["OptnTp"].fillna("").str.strip().replace({"": "FUT"}), "open": _n(raw["OpnPric"]), "high": _n(raw["HghPric"]), "low": _n(raw["LwPric"]),
                          "close": _n(raw["ClsPric"]), "settle": _n(raw["SttlmPric"]), "last": _n(raw["LastPric"]), "prev_close": _n(raw["PrvsClsgPric"]),
                          "underlying_price": _n(raw["UndrlygPric"]), "contracts": _n(raw["TtlTradgVol"]), "value_inr": _n(raw["TtlTrfVal"]), "open_interest": _n(raw["OpnIntrst"]),
                          "chg_oi": _n(raw["ChngInOpnIntrst"]), "trades": _n(raw["TtlNbOfTxsExctd"]), "lot_size": _n(raw["NewBrdLotQty"]), "fin_instrm_id": _n(raw["FinInstrmId"])})
    else:
        raise ValueError(f"unknown F&O bhavcopy format {fmt!r}")
    known = d["instrument"].isin(["FUTIDX", "FUTSTK", "OPTIDX", "OPTSTK"]) & d["option_type"].isin(["FUT", "CE", "PE"])
    n_other = int((~known).sum())
    d = d[known]
    idle = (d["open_interest"].fillna(0) <= 0) & (d["contracts"].fillna(0) <= 0)
    n_idle = int(idle.sum())
    d = d[~idle & d["trade_date"].notna() & d["expiry_date"].notna()].copy()
    for c in ("open", "high", "low"):                                   # untraded-today contracts carry zero O/H/L
        d.loc[d["contracts"].fillna(0) <= 0, c] = np.nan
    d["source_format"], d["source_file"] = fmt, source_file
    d["event_time"] = [datetime.combine(x.date(), datetime.min.time().replace(hour=15, minute=30), tzinfo=timezone(timedelta(hours=5, minutes=30))).astimezone(timezone.utc) for x in d["trade_date"]]
    out = d[OUT_COLS + ["event_time"]].reset_index(drop=True)
    out["_file_rows"], out["_idle"], out["_other"], out["_sha256"] = int(n_file), n_idle, n_other, hashlib.sha256(payload).hexdigest()      # session facts travel with the rows to the sink
    out.attrs.update(n_rows_file=int(n_file), n_dropped_idle=n_idle, n_other_types=n_other)
    return out


class NseFoBhavcopyAdapter(SourceAdapter):
    name, table = "nse_fo_bhavcopy", "fno_eod_raw"
    key_cols = ("trade_date", "instrument", "symbol", "expiry_date", "strike", "option_type")
    slo_seconds = 26 * 3600
    interval_seconds = 1800
    backfill_lag = timedelta(hours=3)
    min_rows = 3000                                      # ~10k rows per session in 2016, ~40k now (idle contracts dropped): fewer means a broken file

    def __init__(self, fetcher=None, now: Callable[[], datetime] = lambda: datetime.now(timezone.utc), start_date: Optional[date] = None, end_date: Optional[date] = None,
                 max_days_per_run: int = 25, landed_sessions=(), extra_dates=(), min_rows: Optional[int] = None):
        self.fetcher = fetcher or HttpFetcher(min_interval_s=1.0)
        self.now, self.start_date, self.end_date, self.max_days = now, start_date, end_date, max_days_per_run
        self.landed, self.no_file, self.extra_dates = set(landed_sessions), set(), set(extra_dates)
        if min_rows is not None:
            self.min_rows = min_rows

    def _candidate_dates(self, since: Optional[datetime]) -> list:
        now = self.now()
        ist = now.astimezone(timezone(timedelta(hours=5, minutes=30)))
        last = _ist_today(now) if ist.hour >= IST_CLOSE_HOUR else _ist_today(now) - timedelta(days=1)
        if self.end_date is not None:
            last = min(last, self.end_date)
        first = self.start_date or (since.astimezone(timezone(timedelta(hours=5, minutes=30))).date() if since else last)
        d, out = first, []
        while d <= last:
            if d.weekday() < 5 and d not in self.landed and d not in self.no_file:
                out.append(d)
            d += timedelta(days=1)
        out += [x for x in sorted(self.extra_dates) if x not in out and x not in self.landed and x not in self.no_file]
        return out[: self.max_days]

    def fetch(self, since: Optional[datetime]) -> list:
        items = []
        for d in self._candidate_dates(since):
            got = None
            for fmt, url in urls_for(d):
                try:
                    payload = self.fetcher.get(url, HEADERS)
                except Exception as e:  # noqa: BLE001 - a missing file is expected on a non-session day
                    log.info("nse fo %s %s: %s", d, fmt, e)
                    continue
                if is_data_file(payload):
                    got = (fmt, url, payload)
                    break
            if got is None:
                self.no_file.add(d)
                continue
            fmt, url, payload = got
            self.landed.add(d)
            items.append(RawItem(payload, self.now(), None, {"url": url, "session_date": str(d), "format": fmt}, "zip"))
        return items

    def parse(self, item: RawItem) -> pd.DataFrame:
        df = parse_fo(item.payload, item.meta["format"], f"nse_fo:{item.meta.get('url')}")
        df.attrs["session"] = date.fromisoformat(item.meta["session_date"])
        return df

    def validate(self, df: pd.DataFrame) -> list:
        errs = super().validate(df)
        if df.empty:
            return errs + ["empty F&O bhavcopy"]
        if len(df) < self.min_rows:
            errs.append(f"only {len(df)} rows (< {self.min_rows}): truncated or wrong file")
        sess = df.attrs.get("session")
        if sess is not None and (df["trade_date"].dt.date != sess).mean() > 0.5:
            errs.append(f"file for {sess} holds {df['trade_date'].dt.date.mode().iloc[0]}")
        if not df["instrument"].isin(["OPTIDX", "OPTSTK"]).any():
            errs.append("no option rows in the file")
        n_other, n_file = int(df["_other"].iloc[0]), int(df["_file_rows"].iloc[0])
        if n_other > 0.02 * max(n_file, 1):
            errs.append(f"{n_other} rows of an unrecognised instrument type")
        live = df[df["open_interest"].fillna(0) > 0]
        bad = (live["settle"].fillna(0) < 0) | (live["strike"] < 0)
        if bad.sum() > 0:
            errs.append(f"{int(bad.sum())} rows with a negative settlement or strike")
        return errs
