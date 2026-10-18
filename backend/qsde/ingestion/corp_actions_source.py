"""NSE corporate-actions source (from the old StockTrack fetch) + price reconciliation.

The old project saved NSE's corporate-action feed as a parquet (26,688 rows, 2014-2026,
~2,700 symbols). Its own parser tags face-value splits as `unsupported` with factor 1.0 (HDFCBANK
2019, IRCTC 2021, ADANIPOWER 2025 ...), but the ratio is in `raw_purpose`; this module re-parses
the text (bonus a:b, face-value split/sub-division From X To Y, consolidation, and combinations).

Authority and verification (method `source_confirmed_v2`):
  * The SOURCE decides that an action happened and its factor; PRICES confirm it. A parsed
    split/bonus is applied only if, within +-2 sessions of the listed ex-date, the overnight gap
    (open / previous close) agrees with the factor. Otherwise it is SOURCE_UNCONFIRMED (logged,
    not applied).
  * This is what makes mild bonuses (1:4, 1:5, 1:10 -> factor 0.8-0.91) adjustable at all: price
    alone cannot tell them from ordinary moves, and a -20% gap is exactly an NSE 20% lower circuit.
  * Demergers / schemes have no parseable factor and stay visible cliffs (listed, not applied).
Rights and cash dividends are NOT adjusted (documented limitation).
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

SOURCE_FILE = Path(os.getenv("STOXSY_CORP_ACTIONS_FILE",
                             r"D:\Stoxsy\data\stocktrack_data\cache\historical\corp_actions.parquet"))
METHOD = "source_confirmed_v2"
SESSION_WINDOW = 2
TOL_STRONG = 0.10        # |open_ratio / factor - 1| for factor <= 0.8 or >= 1.25
TOL_MILD = 0.06          # for mild factors the gap is barely above noise, so be stricter on the match
_NON_EQUITY = re.compile(r"debenture|ncrps?|preference|\bncd\b", re.I)
_NUM = r"(\d+(?:\.\d+)?)"


def parse_purpose(text: Optional[str]) -> dict:
    """-> {'category', 'factor', 'parts'}. category in SPLIT_BONUS | CONSOLIDATION | DEMERGER | OTHER.

    factor = product of all parsed equity components (price multiplier BEFORE the ex-date)."""
    s = (text or "").strip()
    parts: list[tuple[str, float, str]] = []
    if not s:
        return {"category": "OTHER", "factor": None, "parts": parts}

    if re.search(r"demerger|arrangement|arangement|amalgam|merger|capital reduction", s, re.I) \
            and not re.search(r"\bbonus\s*" + _NUM + r"\s*:", s, re.I):
        return {"category": "DEMERGER", "factor": None, "parts": parts}

    for m in re.finditer(r"bonus(?:\s+issue)?\s*" + _NUM + r"\s*:\s*" + _NUM, s, re.I):
        if _NON_EQUITY.search(s):
            return {"category": "OTHER", "factor": None, "parts": []}
        a, b = float(m.group(1)), float(m.group(2))
        if a > 0 and b > 0:
            parts.append(("bonus", b / (a + b), f"bonus {m.group(1)}:{m.group(2)}"))

    is_split = re.search(r"split|sub-?\s*division", s, re.I)
    is_cons = re.search(r"consolidat", s, re.I)
    if is_split or is_cons:
        frm = re.search(r"from\s*(?:face value\s*)?(?:rs|re)?\.?\s*" + _NUM, s, re.I)
        to = re.search(r"\bto\s*(?:face value\s*)?(?:rs|re)?\.?\s*" + _NUM, s, re.I)
        if frm and to and float(frm.group(1)) > 0:
            f = float(to.group(1)) / float(frm.group(1))
            parts.append(("split" if f < 1 else "consolidation", f, f"face value {frm.group(1)} -> {to.group(1)}"))

    if not parts:
        return {"category": "OTHER", "factor": None, "parts": parts}
    factor = float(np.prod([p[1] for p in parts]))
    if not (0.001 < factor < 1000):
        return {"category": "OTHER", "factor": None, "parts": parts}
    cat = "CONSOLIDATION" if factor > 1 else "SPLIT_BONUS"
    return {"category": cat, "factor": factor, "parts": parts}


def load_source(path: Optional[Path] = None) -> pd.DataFrame:
    """Standardised source rows: symbol, ex_date, category, factor, purpose, detail."""
    d = pd.read_parquet(path or SOURCE_FILE)
    d["ex_date"] = pd.to_datetime(d["ex_date"])
    parsed = d["raw_purpose"].map(parse_purpose)
    out = pd.DataFrame({
        "symbol": d["symbol"].astype(str).str.strip(),
        "ex_date": d["ex_date"],
        "category": parsed.map(lambda p: p["category"]),
        "factor": parsed.map(lambda p: p["factor"]),
        "purpose": d["raw_purpose"],
        "detail": parsed.map(lambda p: "; ".join(x[2] for x in p["parts"])),
        "src_type": d["action_type"],
    })
    out = out.drop_duplicates(["symbol", "ex_date", "purpose"]).reset_index(drop=True)
    # Two listed actions on the same ex-date (e.g. a bonus and a split filed as separate rows) are ONE
    # price event: the factors multiply. Without this the events table's unique key is violated.
    adj = out["category"].isin(["SPLIT_BONUS", "CONSOLIDATION"]) & out["factor"].notna()
    dup = adj & out.duplicated(["symbol", "ex_date"], keep=False) & adj
    if dup.any():
        g = (out[dup].groupby(["symbol", "ex_date"], as_index=False)
             .agg(factor=("factor", "prod"), purpose=("purpose", " | ".join), detail=("detail", "; ".join),
                  src_type=("src_type", "first")))
        g["category"] = np.where(g["factor"] > 1, "CONSOLIDATION", "SPLIT_BONUS")
        out = pd.concat([out[~dup], g[out.columns]], ignore_index=True)
    return out.sort_values(["ex_date", "symbol"]).reset_index(drop=True)


def reconcile(source: pd.DataFrame, primary: pd.DataFrame, window: int = SESSION_WINDOW) -> pd.DataFrame:
    """Confirm parsed split/bonus rows against prices. primary: one row per (symbol,date) with open/close.

    Returns events (columns of corp_action_events) with source='nse_corp_actions'.
    """
    d = primary.sort_values(["symbol", "date"]).reset_index(drop=True)
    d["date"] = pd.to_datetime(d["date"])                        # python dates in, Timestamps inside
    sessions = np.sort(d["date"].unique())
    pos = {x: i for i, x in enumerate(sessions)}
    d["sess"] = d["date"].map(pos)
    g = d.groupby("symbol", sort=False)
    d["c0"] = g["close"].shift(1)
    d["d0"] = g["date"].shift(1)
    d["s0"] = g["sess"].shift(1)
    d["r_open"] = d["open"] / d["c0"]
    d["r_close"] = d["close"] / d["c0"]
    d.loc[~((d["sess"] - d["s0"]) == 1), ["r_open", "r_close"]] = np.nan          # need consecutive sessions
    by_sym = {s: grp.set_index("sess") for s, grp in d.groupby("symbol", sort=False)}

    sc = source[source["category"].isin(["SPLIT_BONUS", "CONSOLIDATION"]) & source["factor"].notna()]
    sc = sc[(sc["factor"] - 1).abs() > 0.005]
    first, last = pd.Timestamp(sessions[0]), pd.Timestamp(sessions[-1])
    rows = []
    for r in sc.itertuples(index=False):
        if r.symbol not in by_sym or not (first <= r.ex_date <= last):
            continue
        sd = by_sym[r.symbol]
        p0 = int(np.searchsorted(sessions, np.datetime64(r.ex_date)))            # first session on/after the listed date
        f = float(r.factor)
        tol = TOL_STRONG if (f <= 0.8 or f >= 1.25) else TOL_MILD
        best = None
        for sh in sorted(range(-window, window + 1), key=abs):                   # prefer the listed date
            s = p0 + sh
            if s not in sd.index:
                continue
            ro = sd.at[s, "r_open"]
            if not np.isfinite(ro):
                continue
            err = abs(ro / f - 1.0)
            if best is None or err < best[0] - 1e-12:
                best = (err, s, sh, ro, sd.at[s, "r_close"], sd.at[s, "c0"], sd.at[s, "d0"], sd.at[s, "date"],
                        sd.at[s, "open"], sd.at[s, "close"])
            if err <= tol and sh == 0:
                break
        if best is None:
            rows.append(_row(r, f, None, "SOURCE_UNCONFIRMED", False))
            continue
        confirmed = best[0] <= tol
        kind = ("CONSOLIDATION" if f > 1 else "SPLIT_BONUS") if confirmed else "SOURCE_UNCONFIRMED"
        rows.append(_row(r, f, best, kind, confirmed))
    ev = pd.DataFrame(rows, columns=EVENT_COLS)
    if len(ev):
        # Different listed dates can shift onto the same gap session; one event per (symbol, session):
        # keep the applied one (first), the rest are redundant listings of that same price event.
        ev = (ev.sort_values("applied", ascending=False, kind="mergesort")
                .drop_duplicates(["symbol", "ex_date"], keep="first").reset_index(drop=True))
    return ev


EVENT_COLS = ["exchange", "symbol", "ex_date", "prev_session", "close_prev_session", "open_ex_date",
              "close_ex_date", "open_ratio", "close_ratio", "volume_ratio", "persistence_ratio", "kind",
              "snapped_ratio", "price_factor", "applied", "method", "source", "source_purpose", "ex_date_shift"]
# applied_research / confidence are added by corp_actions.combine_events (source rows: True/'high').


def _row(r, f: float, best, kind: str, applied: bool) -> dict:
    base = {"exchange": "NSE", "symbol": r.symbol, "volume_ratio": np.nan, "persistence_ratio": np.nan,
            "method": METHOD, "source": "nse_corp_actions", "source_purpose": r.purpose[:300]}
    if best is None:
        return {**base, "ex_date": r.ex_date.date(), "prev_session": r.ex_date.date(), "close_prev_session": np.nan,
                "open_ex_date": np.nan, "close_ex_date": np.nan, "open_ratio": np.nan, "close_ratio": np.nan,
                "kind": "SOURCE_UNCONFIRMED", "snapped_ratio": r.detail, "price_factor": np.nan,
                "applied": False, "ex_date_shift": None}
    err, s, sh, ro, rc, c0, d0, dt, op, cl = best
    return {**base, "ex_date": pd.Timestamp(dt).date(), "prev_session": pd.Timestamp(d0).date(),
            "close_prev_session": float(c0), "open_ex_date": float(op), "close_ex_date": float(cl),
            "open_ratio": float(ro), "close_ratio": float(rc), "kind": kind, "snapped_ratio": r.detail,
            "price_factor": f if applied else np.nan, "applied": bool(applied), "ex_date_shift": int(sh)}


def source_version(path: Optional[Path] = None) -> dict:
    """Identity of the corporate-action inputs behind an adjusted series (goes into snapshot manifests)."""
    import hashlib

    p = Path(path or SOURCE_FILE)
    h = hashlib.sha256()
    with open(p, "rb") as f:
        while b := f.read(1 << 20):
            h.update(b)
    return {"file": p.name, "sha256": h.hexdigest(), "bytes": p.stat().st_size, "method": METHOD}
