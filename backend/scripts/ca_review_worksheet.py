"""Event-level review worksheets for RIGHTS issues and DEMERGERS. Nothing here adjusts a price.

Rights and demergers have no universal ratio: a rights issue changes the price only through the (terms-ratio, issue price, market
price) combination, and a demerger moves value into another security whose price series may not even be in our archive. So each
event gets its own row with the evidence a reviewer needs, and a blank `decision` column. A decision is only ever recorded by the
owner (infra/data/corp_action_approvals.json); this script just prepares the evidence.

    python scripts/ca_review_worksheet.py            # writes data/reports/corp_actions/review_rights.csv / review_demergers.csv + .md
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import pandas as pd

OUT = Path(r"D:\Stoxsy\data\reports\corp_actions")


def _bars():
    from qsde.db.connection import read_sql
    from qsde.ingestion import nse_eod

    raw = read_sql("SELECT exchange, symbol, series, date, open, high, low, close, volume FROM eod_raw WHERE exchange = 'NSE'")
    raw["date"] = pd.to_datetime(raw["date"])
    p = nse_eod.pick_primary_series(raw)
    return {s: g.sort_values("date").reset_index(drop=True) for s, g in p.groupby("symbol")}


def _context(g: pd.DataFrame, ex: pd.Timestamp) -> dict:
    """Prices around the ex-date: last close before it, first bar on/after it, the overnight move and 5-session drift."""
    if g is None or g.empty:
        return {"bars": 0}
    before, after = g[g["date"] < ex], g[g["date"] >= ex]
    if before.empty or after.empty:
        return {"bars": len(g)}
    pc, o, c = float(before["close"].iloc[-1]), float(after["open"].iloc[0]), float(after["close"].iloc[0])
    c5 = float(after["close"].iloc[min(4, len(after) - 1)])
    return {"bars": len(g), "last_close_before": pc, "first_bar": after["date"].iloc[0].date(), "gap_open_ratio": o / pc,
            "first_close_ratio": c / pc, "close_5d_ratio": c5 / pc,
            "volume_x": float(after["volume"].iloc[0] / max(before["volume"].tail(20).median(), 1.0))}


def main() -> int:
    from qsde.db.connection import read_sql

    bars = _bars()
    ca = read_sql("""SELECT c.id AS action_id, c.symbol, c.isin, c.company, c.category, c.subject, c.face_value, c.ex_date, c.record_date,
                            r.classification, r.reason, r.symbol_of_day, r.security_id, r.terms, r.amount AS premium, r.gap_date,
                            r.observed_ratio, r.factor_error
                     FROM nse_corporate_actions c
                     JOIN corp_action_reconciliation r ON r.action_id = c.id AND r.reconciler_version = 'ca_reconcile_v1'
                     WHERE c.category IN ('RIGHTS', 'DEMERGER') AND r.classification <> 'rejected' ORDER BY c.ex_date, c.symbol""")
    OUT.mkdir(parents=True, exist_ok=True)
    for cat, name in (("RIGHTS", "rights"), ("DEMERGER", "demergers")):
        d = ca[ca["category"] == cat].copy()
        rows = []
        for r in d.itertuples():
            ctx = _context(bars.get(r.symbol_of_day or r.symbol), pd.Timestamp(r.ex_date))
            row = {**r._asdict(), **ctx}
            row.pop("Index", None)
            if cat == "DEMERGER":
                row["note"] = "value moves to a new/other security; no ratio is inferred; needs the scheme's entitlement + the resulting symbol/ISIN"
            else:
                row["note"] = "price effect depends on terms ratio, issue price and market price; compute the theoretical ex-rights price from the terms and compare"
            row["decision"] = ""                       # filled by the owner only
            rows.append(row)
        df = pd.DataFrame(rows)
        df.to_csv(OUT / f"review_{name}.csv", index=False)
        n_gap = int((df.get("gap_open_ratio", pd.Series(dtype=float)).sub(1).abs() > 0.05).sum()) if len(df) else 0
        md = [f"# {cat.title()} review worksheet ({len(df)} events; no adjustment applied)", "",
              f"- classification: {df['classification'].value_counts().to_dict() if len(df) else {}}",
              f"- events with a >5% overnight move around the ex-date (candidates for a price effect): {n_gap}", "",
              "| ex-date | symbol | ISIN | subject | gap open | close+5d | class |", "|---|---|---|---|---|---|---|"]
        for r in df.sort_values("ex_date", ascending=False).head(60).itertuples():
            md.append(f"| {r.ex_date} | {r.symbol} | {r.isin} | {str(r.subject)[:70]} | "
                      f"{getattr(r, 'gap_open_ratio', float('nan')):.3f} | {getattr(r, 'close_5d_ratio', float('nan')):.3f} | {r.classification} |")
        (OUT / f"review_{name}.md").write_text("\n".join(md), encoding="utf-8")
        print(cat, len(df), "events; >5% moves:", n_gap)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
