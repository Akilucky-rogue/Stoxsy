"""Integrity checks to pass BEFORE ffd_v2s is used for research.

  1. Every owner-approved event is consistent in the official prices AND in the stitched series: the overnight move across its
     gap bar, on the adjusted series, is an ordinary move (< 20%), not a leftover cliff.
  2. No stitched security crosses an unhandled split / bonus / merger / rename: every day with an absolute return >= 25% on the
     STITCHED, adjusted series is listed with any NSE split/bonus/consolidation/demerger record near it and classified:
     NSE_CANDIDATE_NOT_APPLIED (a known action the official series does not adjust) or UNEXPLAINED.

    python scripts/stitched_integrity_check.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from qsde.config.paths import data_path

import numpy as np
import pandas as pd

OUT = data_path("reports", "stitching")
APPROVALS = Path(__file__).resolve().parents[2] / "infra" / "data" / "corp_action_approvals.json"
CLIFF = 0.25


def main() -> int:
    from qsde.db.connection import read_sql
    from qsde.factors import engine
    from qsde.ingestion.security_master import load_from_db

    smap = load_from_db()
    ev = read_sql("SELECT symbol, ex_date, price_factor, applied FROM corp_action_events WHERE applied")
    ev["ex_date"] = pd.to_datetime(ev["ex_date"])
    ca = read_sql("SELECT symbol, ex_date, category, subject FROM nse_corporate_actions "
                  "WHERE category IN ('SPLIT_BONUS','CONSOLIDATION','DEMERGER')")
    ca["ex_date"] = pd.to_datetime(ca["ex_date"])

    def loader(sym, start="2006-01-01", end=None):
        d = read_sql("SELECT date, open, high, low, close, volume FROM ohlcv WHERE symbol = :s ORDER BY date", params={"s": sym})
        d["date"] = pd.to_datetime(d["date"])
        return d.set_index("date")

    # ── 1. approved events in the official prices and in the stitched series
    dec = [d for d in json.loads(APPROVALS.read_text(encoding="utf-8"))["decisions"] if d["decision"] == "approve"]
    rows = []
    for d in dec:
        sym = d.get("apply_to_symbol") or d["symbol"]
        gap = pd.Timestamp(d["gap_date"])
        px = loader(sym)
        i = px.index.searchsorted(gap)
        official = float(px["open"].iloc[i] / px["close"].iloc[i - 1] - 1) if 0 < i < len(px) else np.nan
        sid = smap.security_of(sym)
        stitched = np.nan
        if sid:
            s = engine.stitched_ohlcv(sid, smap, ev, loader=loader)
            j = s.index.searchsorted(gap)
            stitched = float(s["open"].iloc[j] / s["close"].iloc[j - 1] - 1) if 0 < j < len(s) else np.nan
        rows.append({"symbol": d["symbol"], "traded_as": sym, "gap_bar": d["gap_date"], "official_overnight": official,
                     "stitched_security": sid, "stitched_overnight": stitched,
                     "ok_official": abs(official) < 0.20, "ok_stitched": (np.isnan(stitched) or abs(stitched) < 0.20)})
    chk1 = pd.DataFrame(rows)

    # ── 2. unhandled cliffs on every stitched security
    cliffs = []
    for sid, g in smap.intervals.groupby("security_id"):
        if g["symbol"].nunique() < 2:
            continue
        s = engine.stitched_ohlcv(sid, smap, ev, loader=loader)
        if s.empty:
            continue
        r = s["close"].pct_change()
        for dt, v in r[r.abs() >= CLIFF].items():
            syms = list(g["symbol"])
            near = ca[ca["symbol"].isin(syms) & ((ca["ex_date"] - dt).abs() <= pd.Timedelta(days=4))]
            cliffs.append({"security_id": sid, "date": dt.date(), "return": float(v),
                           "status": "NSE_CANDIDATE_NOT_APPLIED" if len(near) else "UNEXPLAINED",
                           "nse_record": near.iloc[0].subject[:60] if len(near) else ""})
    chk2 = pd.DataFrame(cliffs)
    n_sec = int((smap.intervals.groupby("security_id")["symbol"].nunique() >= 2).sum())

    L = ["# Stitched-series integrity check (before using ffd_v2s)", "",
         f"## 1. Approved events: official prices and stitched series ({len(chk1)} events)", "",
         f"- official overnight move across the gap bar < 20%: **{int(chk1['ok_official'].sum())}/{len(chk1)}**",
         f"- stitched overnight move < 20% (where the symbol is part of a stitched security): "
         f"**{int(chk1['ok_stitched'].sum())}/{len(chk1)}**", ""]
    bad = chk1[~(chk1["ok_official"] & chk1["ok_stitched"])]
    for r in bad.itertuples():
        L.append(f"- CHECK {r.symbol} ({r.traded_as}) {r.gap_bar}: official {r.official_overnight:+.1%}, stitched {r.stitched_overnight:+.1%}")
    L += ["", f"## 2. Large moves (>= {CLIFF:.0%}) inside {n_sec} stitched securities", "",
          f"- total: {len(chk2)}; NSE record nearby but NOT applied to prices: "
          f"{int((chk2['status'] == 'NSE_CANDIDATE_NOT_APPLIED').sum()) if len(chk2) else 0}; unexplained (no NSE record): "
          f"{int((chk2['status'] == 'UNEXPLAINED').sum()) if len(chk2) else 0}"]
    for r in (chk2.head(25).itertuples() if len(chk2) else []):
        L.append(f"  - {r.security_id} {r.date} {r.status} {r._3:+.0%} {r.nse_record}")
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "stitched_integrity_check.md").write_text("\n".join(L), encoding="utf-8")
    chk1.to_csv(OUT / "approved_events_consistency.csv", index=False)
    chk2.to_csv(OUT / "stitched_large_moves.csv", index=False)
    print("\n".join(L))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
