"""
NSE F&O Bhavcopy ingest (F&O increment 3) — the free EOD options-history source.

`parse_fo_bhavcopy` is PURE and robust: it handles BOTH the legacy
(INSTRUMENT/SYMBOL/EXPIRY_DT/...) and the current UDiFF
(FinInstrmTp/TckrSymb/XpryDt/...) column schemas, since NSE's archive format
shifted in 2024. Output is a canonical option frame:
    symbol | expiry(date) | strike | opt_type('CE'|'PE') | settle | oi
          | underlying_px | lot_size | instrument

`underlying_px` (UDiFF `UndrlygPric`) and `lot_size` (UDiFF `NewBrdLotQty`) are
carried straight from the file when present (NaN otherwise) — they make the
UDiFF bhavcopy SELF-DESCRIBING, so the chain source derives spot and lot size
from the data instead of hardcoding index structure that NSE keeps changing.

`fetch_fo_bhavcopy` is BEST-EFFORT: NSE blocks non-browser requests, so it warms
a cookie + sets browser headers, but may still fail in restricted environments
(run it on a machine with NSE access). The parser is what's unit-tested.
"""
from __future__ import annotations

import io
import logging
from datetime import date

import pandas as pd

log = logging.getLogger(__name__)

_OPT_MAP = {"CA": "CE", "PU": "PE", "CALL": "CE", "PUT": "PE", "CE": "CE", "PE": "PE"}


def parse_fo_bhavcopy(text: str) -> pd.DataFrame:
    """Parse an NSE F&O bhavcopy CSV (legacy OR UDiFF) into canonical option rows."""
    df = pd.read_csv(io.StringIO(text))
    df.columns = [str(c).strip() for c in df.columns]
    cols = set(df.columns)

    if "OptnTp" in cols or "XpryDt" in cols:                 # UDiFF (current)
        out = pd.DataFrame({
            "symbol": df.get("TckrSymb"),
            "expiry": pd.to_datetime(df.get("XpryDt"), errors="coerce"),
            "strike": pd.to_numeric(df.get("StrkPric"), errors="coerce"),
            "opt_type": df.get("OptnTp"),
            "settle": pd.to_numeric(
                df["SttlmPric"] if "SttlmPric" in cols else df.get("ClsPric"), errors="coerce"),
            "oi": pd.to_numeric(df.get("OpnIntrst"), errors="coerce"),
            # Self-describing UDiFF extras (NaN if a given file omits them).
            "underlying_px": pd.to_numeric(df.get("UndrlygPric"), errors="coerce"),
            "lot_size": pd.to_numeric(df.get("NewBrdLotQty"), errors="coerce"),
            "instrument": df.get("FinInstrmTp"),
        })
    else:                                                    # legacy
        out = pd.DataFrame({
            "symbol": df.get("SYMBOL"),
            "expiry": pd.to_datetime(df.get("EXPIRY_DT"), errors="coerce"),
            "strike": pd.to_numeric(df.get("STRIKE_PR"), errors="coerce"),
            "opt_type": df.get("OPTION_TYP"),
            "settle": pd.to_numeric(
                df["SETTLE_PR"] if "SETTLE_PR" in cols else df.get("CLOSE"), errors="coerce"),
            "oi": pd.to_numeric(df.get("OPEN_INT"), errors="coerce"),
            # Legacy CSVs carry neither underlying price nor lot size.
            "underlying_px": pd.to_numeric(df.get("UNDERLYING_PR"), errors="coerce"),
            "lot_size": pd.to_numeric(df.get("MKT_LOT"), errors="coerce"),
            "instrument": df.get("INSTRUMENT"),
        })

    out["opt_type"] = (out["opt_type"].astype(str).str.strip().str.upper().str[:4]
                       .map(lambda v: _OPT_MAP.get(v, _OPT_MAP.get(v[:2], ""))))
    out = out[out["opt_type"].isin(["CE", "PE"])]
    out = out.dropna(subset=["strike", "settle", "expiry"])
    out["expiry"] = out["expiry"].dt.date
    return out.reset_index(drop=True)


def _underlying_rows(options: pd.DataFrame, underlying: str, expiry=None) -> pd.DataFrame:
    sub = options[options["symbol"].astype(str).str.upper() == underlying.upper()]
    if expiry is not None:
        sub = sub[sub["expiry"] == expiry]
    return sub


def _numeric_col(df: pd.DataFrame, col: str) -> pd.Series:
    """Positive numeric values of `col`, or an empty Series if the column is
    entirely absent (df.get(missing) is None, which pd.to_numeric would choke
    on). Lets the extractors degrade to NaN instead of raising."""
    if col not in df.columns:
        return pd.Series([], dtype="float64")
    s = pd.to_numeric(df[col], errors="coerce").dropna()
    return s[s > 0]


def extract_underlying_spot(options: pd.DataFrame, underlying: str, expiry=None) -> float:
    """Spot for `underlying` (optionally a specific `expiry`) from the bhavcopy's
    own `underlying_px` column (UDiFF `UndrlygPric`). Returns the median across
    the matching option rows (robust to the odd bad print); the index level is
    the same across expiries on a day, so `expiry=None` is the right call for the
    per-underlying IV inversion. NaN if the file didn't carry it (or the column is
    absent) — the caller then falls back to a Kite LTP / futures proxy."""
    px = _numeric_col(_underlying_rows(options, underlying, expiry), "underlying_px")
    return float(px.median()) if len(px) else float("nan")


def extract_lot_size(options: pd.DataFrame, underlying: str, expiry=None) -> float:
    """Contract lot size for `underlying` (optionally `expiry`) from `lot_size`
    (UDiFF `NewBrdLotQty`). Mode of the matching option rows. NaN if absent."""
    lots = _numeric_col(_underlying_rows(options, underlying, expiry), "lot_size")
    if not len(lots):
        return float("nan")
    m = lots.mode()
    return float(m.iloc[0]) if len(m) else float(lots.iloc[0])


def fetch_fo_bhavcopy(d: date, *, timeout: int = 30) -> str:
    """Best-effort download of the UDiFF F&O bhavcopy for date `d`. Returns the
    CSV text. Raises on failure — NSE blocks bots, so run on a machine with NSE
    access (or feed a saved CSV to parse_fo_bhavcopy directly).

    Reuses the india_data NSE session (browser headers + cookie warm + polite
    backoff) — the same plumbing the equity-bhavcopy ground-truth check uses —
    so we don't maintain two divergent NSE clients."""
    import zipfile

    from qsde.ingestion.india_data._common import client, polite_get

    ymd = d.strftime("%Y%m%d")
    url = (f"https://nsearchives.nseindia.com/content/cm/"
           f"BhavCopy_NSE_FO_0_0_0_{ymd}_F_0000.csv.zip")
    with client(headers={"Referer": "https://www.nseindia.com/all-reports-derivatives"}) as c:
        try:
            c.get("https://www.nseindia.com")   # warm the anti-bot cookie
        except Exception:  # noqa: BLE001
            pass
        resp = polite_get(c, url)
        resp.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            return zf.read(zf.namelist()[0]).decode("utf-8", errors="replace")


__all__ = ["parse_fo_bhavcopy", "fetch_fo_bhavcopy",
           "extract_underlying_spot", "extract_lot_size"]
