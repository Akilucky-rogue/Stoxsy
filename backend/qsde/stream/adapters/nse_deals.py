"""NSE bulk and block deals from NSE's official daily files, on the streaming foundation (raw first).

  https://archives.nseindia.com/content/equities/bulk.csv    bulk deals of the latest session   (Date, Symbol, Security Name, Client Name, Buy/Sell, Quantity Traded, Trade Price / Wght. Avg. Price, Remarks)
  https://archives.nseindia.com/content/equities/block.csv   block deals of the latest session  (same without Remarks)

The files are ROLLING: only the latest session is online, so history exists only if we land them every day (run the daily routine; there is no back-fill from these URLs). An empty
file (no deals that day) is valid. Deals are recorded facts about who traded, not a signal; any use as a feature must respect `known_at` (the file appears after the close).
"""
from __future__ import annotations

import hashlib
import io
import logging
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

import pandas as pd

from qsde.stream.adapters.nse import HttpFetcher
from qsde.stream.base import RawItem, SourceAdapter

log = logging.getLogger(__name__)

URLS = {"bulk": "https://archives.nseindia.com/content/equities/bulk.csv", "block": "https://archives.nseindia.com/content/equities/block.csv"}
HEADERS = {"Referer": "https://www.nseindia.com/", "Accept": "*/*"}
IST = timezone(timedelta(hours=5, minutes=30))
OUT_COLS = ["kind", "trade_date", "symbol", "security_name", "client_name", "side", "quantity", "price", "remarks", "seq"]
EXPECTED = {"Date", "Symbol", "Security Name", "Client Name", "Buy/Sell", "Quantity Traded", "Trade Price / Wght. Avg. Price"}


def is_deals_file(payload: bytes) -> bool:
    return payload[:5].lstrip(b"\xef\xbb\xbf") .startswith(b"Date,")


def parse_deals(payload: bytes, kind: str) -> pd.DataFrame:
    """Pure: file bytes -> rows. Raises ValueError on a file that is not a deals file (an HTML error page, a changed layout)."""
    if kind not in URLS:
        raise ValueError(f"unknown deal kind {kind!r}")
    if not is_deals_file(payload):
        raise ValueError("not a deals csv (an error page or a changed layout)")
    raw = pd.read_csv(io.BytesIO(payload), dtype=str, skipinitialspace=True, keep_default_na=False, encoding="utf-8-sig")
    raw.columns = [c.strip() for c in raw.columns]
    missing = EXPECTED - set(raw.columns)
    if missing:
        raise ValueError(f"columns missing from the deals file: {sorted(missing)}")
    d = pd.DataFrame({
        "kind": kind, "trade_date": pd.to_datetime(raw["Date"].str.strip(), format="%d-%b-%Y", errors="coerce"), "symbol": raw["Symbol"].str.strip(),
        "security_name": raw["Security Name"].str.strip(), "client_name": raw["Client Name"].str.strip().str.replace(r"\s+", " ", regex=True),
        "side": raw["Buy/Sell"].str.strip().str.upper(), "quantity": pd.to_numeric(raw["Quantity Traded"].str.replace(",", ""), errors="coerce"),
        "price": pd.to_numeric(raw["Trade Price / Wght. Avg. Price"].str.replace(",", ""), errors="coerce"),
        "remarks": raw["Remarks"].str.strip().replace({"-": None, "": None}) if "Remarks" in raw.columns else None})
    d["_bad"] = d["trade_date"].isna() | d["quantity"].isna() | d["price"].isna() | ~d["side"].isin(["BUY", "SELL"]) | (d["symbol"] == "")
    n_bad = int(d["_bad"].sum())
    d = d[~d["_bad"]].drop(columns="_bad").copy()
    d["quantity"] = d["quantity"].astype("int64")
    d["seq"] = d.groupby(["kind", "trade_date", "symbol", "client_name", "side", "quantity", "price"]).cumcount()
    d["event_time"] = [datetime.combine(x.date(), datetime.min.time().replace(hour=15, minute=30), tzinfo=IST).astimezone(timezone.utc) for x in d["trade_date"]]
    d["_sha256"] = hashlib.sha256(payload).hexdigest()
    d.attrs.update(n_rows_file=int(len(raw)), n_unparsed=n_bad, kind=kind)
    return d[OUT_COLS + ["event_time", "_sha256"]].reset_index(drop=True)


class NseDealsAdapter(SourceAdapter):
    name, table = "nse_deals", "deals_raw"
    key_cols = ("kind", "trade_date", "symbol", "client_name", "side", "quantity", "price", "seq")
    slo_seconds = 30 * 3600
    interval_seconds = 3600

    def __init__(self, fetcher=None, now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.fetcher = fetcher or HttpFetcher(min_interval_s=1.0)
        self.now = now
        self._seen: set = set()

    def fetch(self, since: Optional[datetime]) -> list:
        items = []
        for kind, url in URLS.items():
            try:
                payload = self.fetcher.get(url, HEADERS)
            except Exception as e:  # noqa: BLE001
                log.warning("nse deals %s: %s", kind, e)
                continue
            if not is_deals_file(payload):
                log.warning("nse deals %s: not a deals file (%d bytes)", kind, len(payload))
                continue
            sha = hashlib.sha256(payload).hexdigest()
            if (kind, sha) in self._seen:
                continue
            self._seen.add((kind, sha))
            items.append(RawItem(payload, self.now(), None, {"url": url, "kind": kind}, "csv"))
        return items

    def parse(self, item: RawItem) -> pd.DataFrame:
        return parse_deals(item.payload, item.meta["kind"])

    def validate(self, df: pd.DataFrame) -> list:
        errs = super().validate(df)
        if df.empty:
            return errs                                                  # a session without deals is legitimate
        if df["trade_date"].nunique() > 1:
            errs.append(f"file mixes sessions: {sorted(df['trade_date'].dt.date.astype(str).unique())}")
        if df.attrs.get("n_unparsed", 0) > 0.05 * max(df.attrs.get("n_rows_file", 1), 1):
            errs.append(f"{df.attrs['n_unparsed']} rows could not be parsed")
        if (df["price"] > 1e6).any():
            errs.append("a trade price above 10 lakh: layout shift?")
        return errs
