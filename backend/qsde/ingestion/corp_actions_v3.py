"""source_confirmed_v3: rename-aware, skipped-session-tolerant EVALUATION of listings the v2 reconciler left unconfirmed.

What changes versus v2 (`corp_actions_source.reconcile`):
  1. SYMBOL RESOLUTION  the NSE feed keys an action by the CURRENT symbol; the price evidence lives under the symbol that
     traded on the ex-date. `SecurityMap.symbol_on` (official NSE list + archive hand-off check) maps one to the other.
  2. SKIPPED SESSIONS   the gap is measured against the symbol's last TRADED close, allowing up to MAX_UNTRADED untraded market
     sessions in between (thinly traded / T-group names). v2 required the immediately preceding session.
  3. HARD BOUNDS        the gap bar must be within MAX_SHIFT sessions of the listed ex-date and the factor within 5% (3% for mild
     factors) - the same `check_bounds` the owner-approval path enforces - plus 3-session persistence.

v3 only PRODUCES CANDIDATES. Nothing here touches the official series: a candidate becomes an applied event only through an owner
approval (`corp_actions_approvals`), which re-checks the bounds on the data as it is then.
"""
from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

from qsde.ingestion.corp_actions_approvals import MAX_SHIFT_SESSIONS, MAX_UNTRADED_SESSIONS, check_bounds
from qsde.ingestion.corp_actions_source import parse_purpose
from qsde.ingestion.security_master import SecurityMap

VERSION = "source_confirmed_v3"
PERSIST_TOL = 0.10
PERSIST_SESSIONS = 3

COLS = ["listed_symbol", "symbol_of_day", "security_id", "listed_ex_date", "gap_date", "purpose", "listed_factor",
        "observed_ratio", "factor_error", "shift_sessions", "untraded_sessions", "persistence_ratio", "classification", "reason"]


def evaluate(unconfirmed: pd.DataFrame, primary: pd.DataFrame, smap: Optional[SecurityMap] = None,
             calendar=None) -> pd.DataFrame:
    """unconfirmed: listed_symbol, listed_ex_date, purpose (the NSE text). primary: symbol, date, open, close (raw archive).

    Returns one row per listing with classification v3_candidate | v3_rejected | unresolved and the evidence behind it."""
    px = primary[["symbol", "date", "open", "close"]].copy()
    px["date"] = pd.to_datetime(px["date"])
    # the market calendar must come from ALL symbols, or a halted symbol's untraded days vanish from it
    sessions = np.sort(pd.to_datetime(pd.Series(calendar)).to_numpy()) if calendar is not None else np.sort(px["date"].unique())
    by_sym = {s: g.sort_values("date").reset_index(drop=True) for s, g in px.groupby("symbol", sort=False)}
    rows = []
    for r in unconfirmed.itertuples(index=False):
        listed = pd.Timestamp(r.listed_ex_date)
        parsed = parse_purpose(r.purpose)
        base = dict.fromkeys(COLS)
        base.update(listed_symbol=r.listed_symbol, listed_ex_date=listed.date(), purpose=r.purpose, listed_factor=parsed["factor"])
        if parsed["factor"] is None or parsed["category"] not in ("SPLIT_BONUS", "CONSOLIDATION"):
            rows.append({**base, "classification": "unresolved", "reason": f"no parseable equity factor ({parsed['category']})"})
            continue
        f = float(parsed["factor"])
        sym = smap.symbol_on(r.listed_symbol, listed) if smap is not None else r.listed_symbol
        sid = smap.security_of(r.listed_symbol, None) if smap is not None else None
        base.update(symbol_of_day=sym, security_id=sid)
        b = by_sym.get(sym)
        if b is None or len(b) < 2:
            rows.append({**base, "classification": "unresolved", "reason": f"no price bars for {sym}"})
            continue
        p0 = int(np.searchsorted(sessions, np.datetime64(listed)))
        best = None
        for _, bar in b[(b["date"] >= sessions[max(0, p0 - MAX_SHIFT_SESSIONS)]) &
                        (b["date"] <= sessions[min(len(sessions) - 1, p0 + MAX_SHIFT_SESSIONS)])].iterrows():
            i = int(b.index[b["date"] == bar["date"]][0])
            if i == 0:
                continue
            prev = b.loc[i - 1]
            untraded = int(((sessions > np.datetime64(prev["date"])) & (sessions < np.datetime64(bar["date"]))).sum())
            ratio = float(bar["open"] / prev["close"])
            shift = int(np.searchsorted(sessions, np.datetime64(bar["date"]))) - p0
            err = abs(ratio / f - 1.0)
            if best is None or err < best["err"]:
                post = b.loc[i:i + PERSIST_SESSIONS - 1, "close"]
                pers = float(post.median() / prev["close"]) if len(post) >= PERSIST_SESSIONS else np.nan
                best = dict(err=err, ratio=ratio, shift=shift, untraded=untraded, gap=bar["date"], pers=pers)
        if best is None:
            rows.append({**base, "classification": "unresolved", "reason": f"{sym} has no bars within +-{MAX_SHIFT_SESSIONS} sessions"})
            continue
        viol = check_bounds(f, best["ratio"], best["shift"], best["untraded"])
        persists = np.isfinite(best["pers"]) and abs(best["pers"] / f - 1.0) <= PERSIST_TOL
        if not persists:
            viol.append("price did not persist at the new level (or too little history after the gap)")
        base.update(gap_date=best["gap"].date(), observed_ratio=best["ratio"], factor_error=best["err"],
                    shift_sessions=best["shift"], untraded_sessions=best["untraded"], persistence_ratio=best["pers"])
        if viol:
            rows.append({**base, "classification": "v3_rejected", "reason": "; ".join(viol)})
        else:
            how = f"under {sym} (NSE rename of {r.listed_symbol})" if sym != r.listed_symbol else f"under {sym}"
            rows.append({**base, "classification": "v3_candidate",
                         "reason": f"gap {best['ratio']:.4f} vs {f:.4f} on {best['gap'].date()} {how}; "
                                   f"{best['untraded']} untraded sessions; persists"})
    return pd.DataFrame(rows, columns=COLS)
