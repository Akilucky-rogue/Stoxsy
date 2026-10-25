"""Review of the held corporate-action items against the OFFICIAL NSE record (RECOMMENDATIONS ONLY; applies nothing).

For each held item (UEL, DRCSYSTEMS, NDRAUTO, VERTOZ and the renamed-stock proposals in the 3-5% band) it prints:
  official record  : action text, parsed factor, ex-date, record date, face value, ISIN  (nse_corporate_actions)
  our price record : gap bar, observed ratio, factor error, sessions untraded, how the official ex-date sits relative to the traded bars
and tests a proposed second approval tier B ("official-record-backed"), which the owner may adopt or reject:

  B1  an NSE record for the SAME symbol with a parsed factor equal to the approved factor
  B2  the official ex-date lies after the last pre-gap bar and on/before the gap bar  (the stock simply did not trade in between,
      so every earlier bar is genuinely pre-action)
  B3  observed gap within 10% of the factor and 3-session persistence within 15%

Tier B does NOT change the hard bounds; it is a proposal for when the price check cannot be tight because the stock did not trade.
    python scripts/held_items_review.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from qsde.config.paths import data_path

import numpy as np
import pandas as pd

OUT = data_path("reports", "corp_actions")
APPROVALS = Path(__file__).resolve().parents[2] / "infra" / "data" / "corp_action_approvals.json"


def main() -> int:
    from qsde.db.connection import read_sql
    from qsde.ingestion import nse_eod

    dec = json.loads(APPROVALS.read_text(encoding="utf-8"))["decisions"]
    held = [d for d in dec if d["decision"] in ("manual_review", "rejected_pending_evidence", "reject")] + \
           [d for d in dec if d["symbol"] == "DRCSYSTEMS"]
    rows = []
    for d in held:
        sym = d.get("apply_to_symbol") or d["symbol"]
        api_sym = d["symbol"]
        gap = pd.Timestamp(d["gap_date"])
        f = float(d["factor"])
        api = read_sql("SELECT subject, category, factor, ex_date, record_date, face_value, isin FROM nse_corporate_actions "
                       "WHERE symbol = :s AND category IN ('SPLIT_BONUS','CONSOLIDATION') "
                       "AND ex_date BETWEEN CAST(:a AS DATE) AND CAST(:b AS DATE) ORDER BY ex_date",
                       params={"s": api_sym, "a": str((pd.Timestamp(d["listed_ex_date"]) - pd.Timedelta(days=10)).date()),
                               "b": str((gap + pd.Timedelta(days=10)).date())})
        px = read_sql("SELECT symbol, series, date, open, close FROM eod_raw WHERE symbol = :s AND date BETWEEN CAST(:a AS DATE) AND CAST(:b AS DATE)",
                      params={"s": sym, "a": str((gap - pd.Timedelta(days=60)).date()), "b": str((gap + pd.Timedelta(days=30)).date())})
        px["date"] = pd.to_datetime(px["date"])
        px = nse_eod.pick_primary_series(px).sort_values("date").reset_index(drop=True)
        i = px.index[px["date"] == gap]
        rec = dict(symbol=api_sym, traded_as=sym, decision=d["decision"], listed_ex=d["listed_ex_date"], gap_bar=d["gap_date"], factor=f)
        if len(api):
            a = api.iloc[0]
            rec.update(api_subject=a.subject, api_factor=a.factor, api_ex=str(a.ex_date), api_record=str(a.record_date),
                       api_face_value=a.face_value, isin=a.isin)
        if len(i) and int(i[0]) > 0:
            k = int(i[0])
            prev = px.loc[k - 1]
            ratio = float(px.loc[k, "open"] / prev["close"])
            post = px.loc[k:k + 2, "close"]
            pers = float(post.median() / prev["close"]) if len(post) >= 3 else np.nan
            ex = pd.Timestamp(rec["api_ex"]) if "api_ex" in rec else None
            rec.update(observed=ratio, err=abs(ratio / f - 1), persistence=pers, pers_err=abs(pers / f - 1) if pers == pers else None,
                       prev_bar=str(prev["date"].date()))
            b1 = "api_factor" in rec and rec["api_factor"] is not None and abs(rec["api_factor"] / f - 1) < 1e-6
            b2 = ex is not None and prev["date"] < ex <= gap
            b3 = rec["err"] <= 0.10 and rec["pers_err"] is not None and rec["pers_err"] <= 0.15
            rec.update(B1_official_factor=b1, B2_exdate_in_untraded_window=b2, B3_price_within_10_15=b3,
                       tier_B_eligible=bool(b1 and b2 and b3))
        rows.append(rec)
    df = pd.DataFrame(rows)
    OUT.mkdir(parents=True, exist_ok=True)
    df.to_csv(OUT / "held_items_review.csv", index=False)
    L = ["# Held items vs the official NSE record (recommendations; nothing applied)", "",
         "Tier B (proposal): B1 official factor equals the approved factor; B2 the official ex-date falls in the stock's untraded window "
         "(after its last pre-gap bar, on/before the gap bar); B3 gap within 10% and persistence within 15%.", "",
         "| symbol (traded as) | state | official record | ex / record | gap bar | observed vs factor | untraded window? | B1 | B2 | B3 | tier B |",
         "|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in df.itertuples():
        g = lambda n: getattr(r, n, None)       # noqa: E731
        L.append(f"| {r.symbol}{'' if r.symbol == r.traded_as else ' (' + r.traded_as + ')'} | {r.decision} | {str(g('api_subject'))[:45]} | "
                 f"{g('api_ex')} / {g('api_record')} | {r.gap_bar} | "
                 f"{'' if g('observed') is None else format(g('observed'), '.4f')} vs {r.factor:.4f} "
                 f"({'' if g('err') is None else format(g('err'), '.1%')}) | prev bar {g('prev_bar')} | {g('B1_official_factor')} | "
                 f"{g('B2_exdate_in_untraded_window')} | {g('B3_price_within_10_15')} | **{g('tier_B_eligible')}** |")
    (OUT / "held_items_review.md").write_text("\n".join(L), encoding="utf-8")
    print("\n".join(L))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
