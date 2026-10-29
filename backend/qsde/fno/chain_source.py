"""
Chain sources for the FnoIncomeEngine (F&O increment 4).

The engine is a PURE strike-selection brain that consumes chain snapshots:

    {underlying, spot, t_years, atm_iv, iv_rank, expiry, chain:{(K,'CE'|'PE'):prem},
     lot_size, strike_step, wing_points, atm_strike, source}

This module is the plumbing that PRODUCES those snapshots from real data:

  * `build_snapshots(...)`     — PURE assembler: an IV'd option frame + an
                                 injected ATM-IV-history lookup -> snapshot list.
                                 Picks the front expiry, derives spot/lot from the
                                 data, computes iv_rank lookahead-safely. No I/O.
  * `BhavcopyChainSource`      — EOD/history (free): NSE F&O bhavcopy -> snapshots,
                                 and persists each session's ATM IV so iv_rank has
                                 a real trailing window tomorrow.
  * `KiteChainSource`          — live intraday (best-effort): Kite NFO instruments
                                 + quotes -> snapshots. Pure assembly in
                                 `kite_rows_to_options`; the live fetch is a shell.

The `*ChainSource` objects are callables (`source()` -> list[dict]) so they drop
straight into `FnoIncomeEngine(source=...)`.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Callable, Iterable, Optional

import pandas as pd

from qsde.fno.bhavcopy import (
    extract_lot_size,
    extract_underlying_spot,
    fetch_fo_bhavcopy,
    parse_fo_bhavcopy,
)
from qsde.fno.indices import DEFAULT_INDICES, get_index_spec
from qsde.fno.vol import add_iv, build_chain_snapshot

log = logging.getLogger(__name__)

# IV-history lookup: (underlying, asof) -> trailing ATM-IV values STRICTLY before
# asof. Injected so build_snapshots is pure/testable; production binds it to
# qsde.fno.iv_store.load_atm_iv_history.
IvHistoryLookup = Callable[[str, date], list]


def _select_expiry(sub: pd.DataFrame, asof: date,
                   min_dte: int, max_dte: int) -> Optional[date]:
    """Front expiry to trade: nearest expiry in [asof+min_dte, asof+max_dte] that
    quotes BOTH calls and puts. None if nothing in-window is two-sided.

    min_dte > 0 keeps us off 0-1 DTE gamma cliffs; max_dte caps theta horizon."""
    out: list[date] = []
    for exp, grp in sub.groupby("expiry"):
        dte = (exp - asof).days
        if dte < min_dte or dte > max_dte:
            continue
        kinds = set(grp["opt_type"])
        if {"CE", "PE"}.issubset(kinds):
            out.append(exp)
    return min(out) if out else None


def _nearest_strike(exp_df: pd.DataFrame, spot: float) -> Optional[float]:
    if exp_df.empty:
        return None
    return float(exp_df.iloc[(exp_df["strike"] - spot).abs().argsort().iloc[0]]["strike"])


def build_snapshots(
    options_iv: pd.DataFrame,
    *,
    indices: Iterable[str],
    asof: date,
    iv_history_lookup: IvHistoryLookup,
    min_dte: int = 2,
    max_dte: int = 60,
    source: str = "bhavcopy",
    spot_lookup: Optional[Callable[[str], float]] = None,
) -> list[dict]:
    """PURE: an IV'd option frame -> one engine snapshot per tradeable index.

    `options_iv` needs columns: symbol, expiry(date), strike, opt_type('CE'|'PE'),
    settle, iv, and (for spot/lot) underlying_px, lot_size. `iv_history_lookup`
    returns the trailing ATM-IV window (strictly before `asof`) for iv_rank."""
    snaps: list[dict] = []
    for u in indices:
        u = u.upper()
        try:
            spec = get_index_spec(u)
        except KeyError:
            log.warning("build_snapshots: %s not in INDEX_REGISTRY — skipping", u)
            continue

        sub = options_iv[options_iv["symbol"].astype(str).str.upper() == u]
        if sub.empty:
            continue
        expiry = _select_expiry(sub, asof, min_dte, max_dte)
        if expiry is None:
            log.info("build_snapshots: no two-sided expiry in [%d,%d] DTE for %s",
                     min_dte, max_dte, u)
            continue

        spot = extract_underlying_spot(options_iv, u, expiry)
        if not (spot == spot and spot > 0) and spot_lookup is not None:
            spot = spot_lookup(u)
        if not (spot == spot and spot > 0):
            log.warning("build_snapshots: no usable spot for %s %s — skipping", u, expiry)
            continue

        exp_df = sub[sub["expiry"] == expiry]
        history = list(iv_history_lookup(u, asof) or [])
        snap = build_chain_snapshot(u, spot, asof, expiry, exp_df, history)
        if not (snap["atm_iv"] == snap["atm_iv"]):   # NaN ATM IV -> unusable
            log.warning("build_snapshots: ATM IV NaN for %s %s — skipping", u, expiry)
            continue

        lot = extract_lot_size(options_iv, u, expiry)
        snap["lot_size"] = int(lot) if (lot == lot and lot > 0) else spec.lot_size_default
        snap["strike_step"] = spec.strike_step
        snap["wing_points"] = spec.wing_points
        snap["atm_strike"] = _nearest_strike(exp_df, spot)
        snap["source"] = source
        snap["asof"] = asof                 # session date -> idea-log dedup key
        snap["iv_history_n"] = len(history)  # trailing sample size -> warm-up gate
        snaps.append(snap)
    return snaps


def db_atm_iv_history(underlying: str, asof: date, lookback_days: int) -> list:
    """The production IvHistoryLookup: the persisted trailing ATM-IV window
    (strictly before `asof`). Shared by both chain sources so there's one DB-read
    path; injected as a plain function in tests to keep build_snapshots pure."""
    from qsde.fno.iv_store import load_atm_iv_history
    return load_atm_iv_history(underlying, asof, lookback_days=lookback_days)


def persist_atm_iv(snaps: list[dict], asof: date, *, source: str) -> int:
    """Record each snapshot's ATM IV to fno_iv_history (idempotent per day).
    This is what grows iv_rank's trailing window. Persistence failures are
    logged, never raised — they must not kill a scan."""
    from qsde.fno.iv_store import record_atm_iv
    n = 0
    for s in snaps:
        try:
            if record_atm_iv(
                s["underlying"], asof, s["expiry"], s["atm_iv"],
                spot=s.get("spot"), atm_strike=s.get("atm_strike"),
                t_years=s.get("t_years"), source=source,
            ):
                n += 1
        except Exception as e:  # noqa: BLE001
            log.warning("persist_atm_iv: failed for %s: %s", s.get("underlying"), e)
    return n


def _add_iv_per_underlying(options: pd.DataFrame, indices: Iterable[str],
                           asof: date) -> pd.DataFrame:
    """add_iv inverts each row's settle at a single spot, but each index has its
    OWN spot — so invert per underlying (the index level is flat across expiries
    on a day). Rows whose underlying has no usable spot are dropped."""
    frames = []
    for u in indices:
        u = u.upper()
        sub = options[options["symbol"].astype(str).str.upper() == u]
        if sub.empty:
            continue
        spot = extract_underlying_spot(options, u, expiry=None)
        if not (spot == spot and spot > 0):
            log.warning("_add_iv_per_underlying: no spot for %s — dropping its rows", u)
            continue
        frames.append(add_iv(sub, spot=spot, asof=asof))
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


class BhavcopyChainSource:
    """EOD chain source from the NSE F&O bhavcopy (free options history).

    Call it (`source()`) to get the engine snapshots for `asof`. If `persist` is
    on, it also records each index's ATM IV to `fno_iv_history`, building the
    trailing window iv_rank reads tomorrow. Feed `csv_text` directly (a saved
    bhavcopy) for backtests / where NSE is blocked; otherwise it fetches.
    """

    def __init__(self, asof: date, *, indices: Iterable[str] = DEFAULT_INDICES,
                 csv_text: Optional[str] = None, lookback_days: int = 252,
                 min_dte: int = 2, max_dte: int = 60, persist: bool = True) -> None:
        self.asof = asof
        self.indices = list(indices)
        self.csv_text = csv_text
        self.lookback_days = int(lookback_days)
        self.min_dte = int(min_dte)
        self.max_dte = int(max_dte)
        self.persist = bool(persist)

    def _history_lookup(self, underlying: str, asof: date) -> list:
        return db_atm_iv_history(underlying, asof, self.lookback_days)

    def __call__(self) -> list[dict]:
        if self.csv_text is not None:
            text = self.csv_text
        else:
            # NSE blocks bots / hasn't published the file yet (403/404) — degrade
            # to "no chain today" rather than crashing the caller, mirroring
            # KiteChainSource. The daily run / standalone script then logs 0 ideas.
            try:
                text = fetch_fo_bhavcopy(self.asof)
            except Exception as e:  # noqa: BLE001
                log.warning("BhavcopyChainSource: bhavcopy unavailable for %s (%s) — "
                            "no chain this run", self.asof, e)
                return []
        options = parse_fo_bhavcopy(text)
        if options.empty:
            log.warning("BhavcopyChainSource: empty option frame for %s", self.asof)
            return []
        iv_df = _add_iv_per_underlying(options, self.indices, self.asof)
        if iv_df.empty:
            return []
        snaps = build_snapshots(
            iv_df, indices=self.indices, asof=self.asof,
            iv_history_lookup=self._history_lookup,
            min_dte=self.min_dte, max_dte=self.max_dte, source="bhavcopy",
        )
        if self.persist:
            persist_atm_iv(snaps, self.asof, source="bhavcopy")
        return snaps


def kite_rows_to_options(instruments: list[dict], quotes: dict, spot: float,
                         underlying: str) -> pd.DataFrame:
    """PURE: Kite NFO option instruments + a quote map -> the canonical option
    frame build_snapshots expects.

    `instruments`: rows with tradingsymbol, strike, instrument_type('CE'/'PE'),
    expiry(date), lot_size. `quotes`: {tradingsymbol_or_key: {'last_price': ...}}.
    Premium = last_price; rows without a quote are dropped."""
    rows = []
    for inst in instruments:
        ts = inst.get("tradingsymbol")
        ot = str(inst.get("instrument_type", "")).upper()
        if ot not in ("CE", "PE"):
            continue
        strike = inst.get("strike")
        if strike is None:                       # malformed instrument row
            continue
        q = quotes.get(ts) or quotes.get(f"NFO:{ts}")
        if not q:
            continue
        prem = q.get("last_price")
        if prem is None or float(prem) <= 0:
            continue
        rows.append({
            "symbol": underlying.upper(),
            "expiry": inst.get("expiry"),
            "strike": float(strike),
            "opt_type": ot,
            "settle": float(prem),
            "underlying_px": float(spot),
            "lot_size": inst.get("lot_size"),
            "instrument": "OPTIDX",
        })
    df = pd.DataFrame(rows)
    if not df.empty:
        df["expiry"] = pd.to_datetime(df["expiry"], errors="coerce").dt.date
        df = df.dropna(subset=["expiry", "strike", "settle"]).reset_index(drop=True)
    return df


class KiteChainSource:
    """Live intraday chain source (best-effort): Kite NFO instruments + quotes.

    The pure assembly is `kite_rows_to_options`; this wraps it with the live
    Kite calls (needs the daily OAuth token + NFO instruments). Where the token
    is missing it returns [] rather than raising, so the engine just sits idle.
    """

    def __init__(self, asof: date, *, indices: Iterable[str] = DEFAULT_INDICES,
                 lookback_days: int = 252, min_dte: int = 2, max_dte: int = 60,
                 client=None, persist: bool = True) -> None:
        self.asof = asof
        self.indices = list(indices)
        self.lookback_days = int(lookback_days)
        self.min_dte = int(min_dte)
        self.max_dte = int(max_dte)
        self._client = client
        self.persist = bool(persist)

    def _history_lookup(self, underlying: str, asof: date) -> list:
        return db_atm_iv_history(underlying, asof, self.lookback_days)

    def _kite(self):
        if self._client is None:
            from qsde.ingestion.kite_client import get_kite_client
            self._client = get_kite_client()
        return self._client

    def __call__(self) -> list[dict]:
        try:
            kc = self._kite()
        except Exception as e:  # noqa: BLE001
            log.warning("KiteChainSource: no Kite client (%s) — idle", e)
            return []
        if not getattr(kc, "is_authenticated", False):
            log.warning("KiteChainSource: Kite not authenticated — idle")
            return []

        frames = []
        for u in self.indices:
            u = u.upper()
            try:
                spec = get_index_spec(u)
                spot = float(kc.ltp([spec.spot_symbol]).get(spec.spot_symbol))
                insts = self._nfo_instruments(kc, u)
                # Quote only strikes within ~2.5 expected-moves of spot to stay
                # under Kite's per-call instrument cap; cheap and sufficient.
                near = [i for i in insts if i.get("strike") is not None
                        and abs(float(i["strike"]) - spot)
                        <= 2.5 * max(spec.strike_step, 0.04 * spot)]
                keys = [f"NFO:{i['tradingsymbol']}" for i in near]
                quotes = kc.quote(keys) if keys else {}
                quotes = {k.split(":", 1)[-1]: v for k, v in quotes.items()}
                frames.append(kite_rows_to_options(near, quotes, spot, u))
            except Exception as e:  # noqa: BLE001
                log.warning("KiteChainSource: %s failed (%s) — skipping", u, e)
        options = pd.concat([f for f in frames if not f.empty], ignore_index=True) \
            if any(not f.empty for f in frames) else pd.DataFrame()
        if options.empty:
            return []

        iv_df = _add_iv_per_underlying(options, self.indices, self.asof)
        snaps = build_snapshots(
            iv_df, indices=self.indices, asof=self.asof,
            iv_history_lookup=self._history_lookup,
            min_dte=self.min_dte, max_dte=self.max_dte, source="kite",
        )
        if self.persist:
            persist_atm_iv(snaps, self.asof, source="kite")
        return snaps

    @staticmethod
    def _nfo_instruments(kc, underlying: str) -> list[dict]:
        """All NFO option instruments for `underlying` via the SDK. Best-effort."""
        raw = kc._kite.instruments("NFO")           # noqa: SLF001 — SDK passthrough
        out = []
        for r in raw:
            if str(r.get("name", "")).upper() != underlying.upper():
                continue
            if str(r.get("instrument_type", "")).upper() not in ("CE", "PE"):
                continue
            exp = r.get("expiry")
            out.append({
                "tradingsymbol": r.get("tradingsymbol"),
                "strike": r.get("strike"),
                "instrument_type": r.get("instrument_type"),
                "expiry": exp.date() if hasattr(exp, "date") else exp,
                "lot_size": r.get("lot_size"),
            })
        return out


__all__ = [
    "build_snapshots",
    "persist_atm_iv",
    "BhavcopyChainSource",
    "KiteChainSource",
    "kite_rows_to_options",
    "IvHistoryLookup",
]
