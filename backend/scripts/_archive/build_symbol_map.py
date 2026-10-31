"""Candidate symbol-change map (old symbol -> new symbol) derived from the price archive. UNVERIFIED until an NSE
symbol-change source (or Kite instruments + a human) confirms each row. Nothing here changes any price series.

Why: the NSE corporate-action feed keys actions by the CURRENT symbol while `eod_raw` holds the symbol of the day, so a
renamed stock is two separate series and its splits/bonuses sit under the wrong key.

Evidence for a candidate pair (old, new):
  * a split/bonus-sized gap with the listed factor appears under `old` on the listed date (from the reconciliation)
  * the two series are time-adjacent and DO NOT overlap: old's last bar precedes new's first bar by <= 15 sessions.
    Two stocks that merely show the same ratio on the same day (a chance twin) trade concurrently, so they fail this test.
  * ISIN equality where the archive carries one (2016 - mid 2020 only)
Writes D:\\Stoxsy\\data\\reports\\corp_actions\\symbol_changes_candidate.csv and, with --load, the `symbol_changes` table.

    python scripts/build_symbol_map.py [--load]
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from qsde.config.paths import data_path

import numpy as np
import pandas as pd

REPORT = data_path("reports", "corp_actions", "source_unconfirmed_reconciliation.csv")
OUT = data_path("reports", "corp_actions", "symbol_changes_candidate.csv")
HANDOFF_SESSIONS = 15


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--load", action="store_true", help="write the candidates to the symbol_changes table")
    args = ap.parse_args()
    from qsde.db.connection import read_sql

    rec = pd.read_csv(REPORT)
    rec = rec[rec["reason"].fillna("").str.contains("matching gap exists under")]
    cand = []
    for r in rec.itertuples():
        for m in re.finditer(r"([A-Z0-9&\-]+)@(\d{4}-\d{2}-\d{2}) ratio ([\d.]+)", r.reason):
            cand.append({"new_symbol": r.symbol, "old_symbol": m.group(1), "event_date": m.group(2),
                         "ratio": float(m.group(3)), "listed_factor": r.listed_factor, "purpose": r.purpose})
    cand = pd.DataFrame(cand)
    syms = sorted(set(cand["new_symbol"]) | set(cand["old_symbol"]))
    bars = read_sql("SELECT symbol, min(date) AS first_bar, max(date) AS last_bar, count(*) AS n, "
                    "count(isin) AS n_isin, min(isin) AS isin FROM eod_raw WHERE symbol = ANY(:s) GROUP BY symbol",
                    params={"s": syms}).set_index("symbol")
    cal = read_sql("SELECT DISTINCT date FROM eod_raw ORDER BY date")["date"]
    cal = pd.to_datetime(cal).to_numpy()

    def sessions_between(a, b) -> int:
        return int(((cal > np.datetime64(pd.Timestamp(a))) & (cal < np.datetime64(pd.Timestamp(b)))).sum())

    rows = []
    for r in cand.itertuples():
        if r.old_symbol not in bars.index:
            continue
        o = bars.loc[r.old_symbol]
        n = bars.loc[r.new_symbol] if r.new_symbol in bars.index else None
        evidence, status = [], "rejected_chance_twin"
        if n is None:
            evidence.append("new symbol has NO bars in the archive")
            status = "candidate_new_symbol_absent"
        else:
            overlap = pd.Timestamp(n["first_bar"]) <= pd.Timestamp(o["last_bar"])
            gap = sessions_between(o["last_bar"], n["first_bar"]) if not overlap else -1
            evidence.append(f"old {pd.Timestamp(o['first_bar']).date()}..{pd.Timestamp(o['last_bar']).date()}, "
                            f"new {pd.Timestamp(n['first_bar']).date()}..{pd.Timestamp(n['last_bar']).date()}")
            if not overlap and 0 <= gap <= HANDOFF_SESSIONS:
                status = "candidate_handoff_consistent"
                evidence.append(f"series are adjacent ({gap} sessions between old's last and new's first bar)")
            elif overlap:
                evidence.append("series OVERLAP in time -> two live stocks, not a rename")
            else:
                evidence.append(f"{gap} sessions between series (> {HANDOFF_SESSIONS})")
        isin_o, isin_n = (o["isin"] if o["n_isin"] else None), (n["isin"] if n is not None and n["n_isin"] else None)
        if isin_o and isin_n:
            evidence.append(f"ISIN {'equal' if isin_o == isin_n else 'DIFFERENT'} ({isin_o} / {isin_n})")
        rows.append({"old_symbol": r.old_symbol, "new_symbol": r.new_symbol,
                     "effective_after": pd.Timestamp(o["last_bar"]).date(),
                     "new_first_bar": None if n is None else pd.Timestamp(n["first_bar"]).date(),
                     "event_date": r.event_date, "event_ratio": r.ratio, "listed_factor": r.listed_factor,
                     "isin_old": isin_o, "isin_new": isin_n, "status": status, "evidence": "; ".join(evidence),
                     "source": "price-archive inference (UNVERIFIED)", "purpose": r.purpose})
    out = pd.DataFrame(rows).drop_duplicates(["old_symbol", "new_symbol"]).sort_values(["status", "new_symbol"])
    OUT.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(OUT, index=False)
    print(out["status"].value_counts().to_string())
    print(out[out["status"] == "candidate_handoff_consistent"][["old_symbol", "new_symbol", "effective_after", "new_first_bar"]].to_string(index=False))

    if args.load:
        from qsde.db.connection import execute_sql
        execute_sql("DELETE FROM symbol_changes WHERE verified = FALSE")
        for r in out[out["status"].str.startswith("candidate")].itertuples():
            execute_sql("""INSERT INTO symbol_changes (old_symbol, new_symbol, effective_after, status, isin_old, isin_new,
                               evidence, source, verified)
                           VALUES (%(o)s, %(n)s, %(e)s, %(st)s, %(io)s, %(in)s, %(ev)s, %(src)s, FALSE)
                           ON CONFLICT (old_symbol, new_symbol) DO NOTHING""",
                        {"o": r.old_symbol, "n": r.new_symbol, "e": r.effective_after, "st": r.status,
                         "io": r.isin_old, "in": r.isin_new, "ev": r.evidence, "src": r.source})
        print("loaded candidates into symbol_changes (verified = FALSE)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
