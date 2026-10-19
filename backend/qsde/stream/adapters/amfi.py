"""AMFI NAV files on the streaming foundation (official AMFI portal).

  history   https://portal.amfiindia.com/DownloadNAVHistoryReport_Po.aspx?frmdt=<dd-Mon-yyyy>&todt=<dd-Mon-yyyy>   (all schemes, one date window)
            columns: Scheme Code;NAV Name;Plan;Option;ISIN Div Payout/ISIN Growth;ISIN Div Reinvestment;Net Asset Value;Date
  navall    https://portal.amfiindia.com/spages/NAVAll.txt                                                        (latest NAV of every scheme)
            columns: Scheme Code;ISIN Div Payout/ ISIN Growth;ISIN Div Reinvestment;Scheme Name;Plan;Option;Net Asset Value;Date
Both files interleave the data with section lines: a category heading (e.g. `Open Ended Schemes(Debt Scheme - Banking and PSU Fund)`) and an AMC line
(`Axis Mutual Fund`). The parser carries them forward onto each scheme row. Raw bytes are landed first; a NAV of `N.A.` or blank is dropped.
"""
from __future__ import annotations

import hashlib
import io
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Optional

import numpy as np
import pandas as pd

from qsde.stream.adapters.nse import HttpFetcher
from qsde.stream.base import RawItem, SourceAdapter

log = logging.getLogger(__name__)

URL_HISTORY = "https://portal.amfiindia.com/DownloadNAVHistoryReport_Po.aspx?frmdt={a}&todt={b}"
URL_NAVALL = "https://portal.amfiindia.com/spages/NAVAll.txt"
OUT_COLS = ["scheme_code", "date", "nav", "name", "amc", "category_raw", "plan", "option_type", "isin_a", "isin_b"]
ISIN_RE = r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$"


def _fmt(d: date) -> str:
    return d.strftime("%d-%b-%Y")


def is_amfi_data(payload: bytes) -> bool:
    """A real AMFI file starts with its column header; for an oversized or failing window the portal answers HTTP 200 with an HTML page."""
    head = payload[:300].lstrip().lower()
    return head.startswith(b"scheme code") and (b"nav name" in head or b"scheme name" in head)


MIN_SPLIT_DAYS = 7


def parse_amfi(payload: bytes) -> pd.DataFrame:
    """History or NAVAll file -> OUT_COLS (+ event_time). Section lines are carried forward as category / AMC."""
    txt = payload.decode("utf-8", errors="replace")
    lines = txt.splitlines()
    if not lines:
        raise ValueError("empty AMFI file")
    head = lines[0].lower()
    history = "nav name" in head
    if not history and "scheme name" not in head:
        raise ValueError(f"unrecognised AMFI header: {lines[0][:80]}")
    cat, amc = None, None
    rows = []
    for ln in lines[1:]:
        if not ln.strip():
            continue
        if ";" not in ln:                                                   # section line
            t = ln.strip()
            if t.lower().startswith(("open ended", "close ended", "interval")):
                cat = t
            else:
                amc = t
            continue
        f = ln.split(";")
        if len(f) < 8:
            continue
        rows.append((f[0], f[1], f[2], f[3], f[4], f[5], f[6], f[7], cat, amc))
    if not rows:
        raise ValueError("no scheme rows in AMFI file")
    raw = pd.DataFrame(rows, columns=["c0", "c1", "c2", "c3", "c4", "c5", "nav", "dt", "cat", "amc"])
    if history:                                                              # code;name;plan;option;isinA;isinB;nav;date
        df = pd.DataFrame({"scheme_code": raw["c0"], "name": raw["c1"], "plan": raw["c2"], "option_type": raw["c3"], "isin_a": raw["c4"], "isin_b": raw["c5"]})
    else:                                                                    # code;isinA;isinB;name;plan;option;nav;date
        df = pd.DataFrame({"scheme_code": raw["c0"], "name": raw["c3"], "plan": raw["c4"], "option_type": raw["c5"], "isin_a": raw["c1"], "isin_b": raw["c2"]})
    df["nav"] = pd.to_numeric(raw["nav"], errors="coerce")
    df["date"] = pd.to_datetime(raw["dt"], format="%d-%b-%Y", errors="coerce")
    df["category_raw"], df["amc"] = raw["cat"], raw["amc"]
    df["scheme_code"] = pd.to_numeric(df["scheme_code"], errors="coerce")
    for c in ("isin_a", "isin_b"):
        df[c] = df[c].str.strip().where(df[c].str.strip().str.match(ISIN_RE, na=False), None)
    for c in ("name", "plan", "option_type"):
        s = df[c].str.strip()
        df[c] = s.where(s != "", None)
    n0 = len(df)
    df = df[df["scheme_code"].notna() & df["date"].notna() & (df["nav"] > 0)].copy()
    df["scheme_code"] = df["scheme_code"].astype(int)
    df = df.drop_duplicates(["scheme_code", "date"], keep="last")
    df.attrs["dropped_rows"] = int(n0 - len(df))
    df["event_time"] = df["date"].map(lambda d: datetime(d.year, d.month, d.day, 17, 30, tzinfo=timezone.utc))      # 23:00 IST on the NAV date
    out = df[OUT_COLS + ["event_time"]]
    out.attrs["dropped_rows"] = df.attrs["dropped_rows"]
    return out


class AmfiNavAdapter(SourceAdapter):
    name, table = "amfi_nav", "mf_nav"
    key_cols = ("scheme_code", "date")
    slo_seconds = 40 * 3600
    interval_seconds = 6 * 3600
    backfill_lag = timedelta(hours=6)           # AMFI publishes the day's NAVs in the evening / night
    min_rows = 500

    def __init__(self, fetcher=None, now: Callable[[], datetime] = lambda: datetime.now(timezone.utc), windows: Optional[list] = None,
                 landed_windows=(), navall: bool = False):
        self.fetcher = fetcher or HttpFetcher(min_interval_s=3.0, timeout_s=420)
        self.now, self.windows, self.navall = now, windows, navall
        self.landed = set(landed_windows)

    @staticmethod
    def quarter_windows(start: date, end: date, months: int = 3) -> list:
        out, d = [], start
        while d <= end:
            nxt = (pd.Timestamp(d) + pd.DateOffset(months=months)).date() - timedelta(days=1)
            out.append((d, min(nxt, end)))
            d = nxt + timedelta(days=1)
        return out

    def fetch(self, since: Optional[datetime]) -> list:
        if self.navall:
            return [RawItem(self.fetcher.get(URL_NAVALL), self.now(), None, {"url": URL_NAVALL, "kind": "navall"}, "txt")]
        items: list = []

        def get(a: date, b: date) -> None:
            """Fetch one window; if the portal answers with a page instead of data, split it in halves (down to a week) and retry each half."""
            if (a, b) in self.landed or len(items) >= 4:                # bounded work per run: the runner loops until nothing new
                return
            url = URL_HISTORY.format(a=_fmt(a), b=_fmt(b))
            payload = self.fetcher.get(url)
            if is_amfi_data(payload):
                self.landed.add((a, b))
                items.append(RawItem(payload, self.now(), None, {"url": url, "kind": "history", "from": str(a), "to": str(b)}, "txt"))
                return
            if (b - a).days < MIN_SPLIT_DAYS:
                raise ValueError(f"AMFI returned a non-data page for {a}..{b} even at the smallest window")
            mid = a + (b - a) / 2
            get(a, mid)
            get(mid + timedelta(days=1), b)

        for a, b in (self.windows or []):
            get(a, b)
        return items

    def parse(self, item: RawItem) -> pd.DataFrame:
        df = parse_amfi(item.payload)
        df["source_sha256"] = hashlib.sha256(item.payload).hexdigest()
        df["window_from"] = item.meta.get("from")
        df["window_to"] = item.meta.get("to")
        df["kind"] = item.meta.get("kind", "history")
        return df

    def validate(self, df: pd.DataFrame) -> list:
        errs = super().validate(df)
        if len(df) < self.min_rows:
            errs.append(f"only {len(df)} NAV rows (< {self.min_rows}): truncated or wrong file")
        if df.attrs.get("dropped_rows", 0) > 0.02 * (len(df) + df.attrs.get("dropped_rows", 0)):
            errs.append(f"{df.attrs['dropped_rows']} rows dropped (N.A. / bad rows) - more than 2%")
        return errs
