"""READ-ONLY reconciliation of NSE-listed split/bonus rows that prices did not confirm (SOURCE_UNCONFIRMED).

Nothing is written to the database and nothing is applied: the output is a review file that puts every
listing in one of three buckets.

  confirm_and_apply            a clean, persistent gap matching the listed ratio exists within +-WIDE sessions of
                               the listing, ISIN unchanged. Applying it (at the observed date) is the proposal.
  needs_manual_review          evidence is partial or contradictory: ISIN change, a gap of the wrong size, a
                               demerger/rights/scheme in the purpose text, too little price history, etc.
  reject_listing_or_data_mismatch   no price evidence of the action anywhere near the listed date (stale or
                               duplicated listing, already-adjusted archive, parser miss, wrong symbol/date)

Evidence per listing: NSE purpose text + parsed factor, best open-gap and its offset, close-persistence after the
gap, ISIN before/after, trading-day coverage around the date. Fields NSE did not provide in the old feed
(record date, face value, ISIN) are reported as unavailable instead of guessed.

    python scripts/reconcile_source_unconfirmed.py            # writes D:\\Stoxsy\\data\\reports\\corp_actions\\
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import pandas as pd

WIDE = 10                  # sessions searched around the listed date
FLAT_BAND = 0.04           # |open_ratio - 1| below this everywhere in the window == no gap at all
TOL = 0.10                 # factor match tolerance (same as the reconciler's strong tolerance)
PERSIST_SESSIONS = 5
TOL_MILD = 0.06            # mild factors (0.8-1.25): the gap barely clears noise, so match more tightly
MAX_SHIFT = 5              # sessions the observed gap may sit from the listed ex-date for an apply proposal
OUT = Path(r"D:\Stoxsy\data\reports\corp_actions")
RIGHTS = re.compile(r"right|warrant|partly paid|scheme|arrang|amalgam|merger|demerg|capital reduction|buyback", re.I)

BUCKETS = ("confirm_and_apply", "needs_manual_review", "reject_listing_or_data_mismatch")


def evidence(row, px: pd.DataFrame, factor: float) -> dict:
    """Price/ISIN evidence for one listing. px: primary series for the symbol (date-sorted, one row/day)."""
    px = px.sort_values("date").reset_index(drop=True)
    ex = pd.Timestamp(row.listed_ex)
    out = {"n_bars": len(px), "first_bar": px["date"].min() if len(px) else None,
           "last_bar": px["date"].max() if len(px) else None}
    if px.empty:
        return {**out, "coverage": "no_bars"}
    i0 = int(np.searchsorted(px["date"].to_numpy(), np.datetime64(ex)))
    lo, hi = max(1, i0 - WIDE), min(len(px) - 1, i0 + WIDE)
    out["coverage"] = "ok" if (i0 - WIDE >= 1 and i0 + WIDE + PERSIST_SESSIONS < len(px)) else "edge_of_data"
    if lo > hi:
        return {**out, "coverage": "no_bars"}
    win = px.iloc[lo:hi + 1].copy()
    prev_close = px["close"].shift(1).reindex(win.index)
    win["r_open"] = win["open"] / prev_close
    win["r_close"] = win["close"] / prev_close
    win["err"] = (win["r_open"] / factor - 1.0).abs()
    best = win.loc[win["err"].idxmin()]
    bi = int(best.name)
    after = px.iloc[bi:bi + PERSIST_SESSIONS + 1]["close"]
    persist = float(after.median() / px.iloc[bi - 1]["close"]) if len(after) > 2 else np.nan
    isin_b = px.iloc[max(0, i0 - WIDE):i0]["isin"].dropna().unique() if "isin" in px else []
    isin_a = px.iloc[i0:i0 + WIDE]["isin"].dropna().unique() if "isin" in px else []
    out.update(
        best_gap_date=best["date"], best_gap_offset_sessions=bi - i0, best_open_ratio=float(best["r_open"]),
        best_close_ratio=float(best["r_close"]), best_err=float(best["err"]), persistence_ratio=persist,
        window_min_open_ratio=float(win["r_open"].min()), window_max_open_ratio=float(win["r_open"].max()),
        flat_window=bool(((win["r_open"] - 1).abs() < FLAT_BAND).all()),
        isin_before="|".join(map(str, isin_b)), isin_after="|".join(map(str, isin_a)),
        isin_changed=bool(len(isin_b) and len(isin_a) and set(isin_b) != set(isin_a)),
    )
    return out


def alias_candidates(listed_ex, factor: float, target: str, prim_all: pd.DataFrame, isin_map: dict) -> list:
    """Symbols (other than `target`) that show a gap matching `factor` within +-WIDE sessions of the listed date.

    A corporate action filed under today's symbol often traded under an older one (renames, mergers): the price
    evidence then sits in the OLD symbol's series. Only strong factors (<= 0.8 or >= 1.25) are searched: for mild
    ones a market-wide search is dominated by chance matches. `isin_overlap` is only knowable for archive years that
    carry an ISIN (2016 - mid 2020); 'n/a' otherwise.
    """
    if not (factor <= 0.8 or factor >= 1.25):
        return []
    lo = pd.Timestamp(listed_ex) - pd.Timedelta(days=16)
    hi = pd.Timestamp(listed_ex) + pd.Timedelta(days=16)
    w = prim_all[(prim_all["date"] >= lo) & (prim_all["date"] <= hi) & (prim_all["symbol"] != target)]
    w = w[(w["prev_close"] > 0) & (w["open"] > 0)].copy()
    w["ro"] = w["open"] / w["prev_close"]
    w["err"] = (w["ro"] / factor - 1.0).abs()
    hit = w[w["err"] <= TOL].sort_values("err").drop_duplicates("symbol").head(5)
    t_isins = isin_map.get(target, set())
    out = []
    for r in hit.itertuples():
        isins = isin_map.get(r.symbol, set())
        overlap = ("yes" if (t_isins & isins) else "no") if (t_isins and isins) else "n/a"
        out.append(f"{r.symbol}@{pd.Timestamp(r.date).date()} ratio {r.ro:.3f} isin_overlap={overlap}")
    return out


def classify(purpose: str, factor: float, ev: dict) -> tuple[str, str]:
    """-> (bucket, reason). Conservative: the apply bucket needs positive price evidence on every axis."""
    if ev.get("coverage") == "no_bars":
        return "reject_listing_or_data_mismatch", "no_price_bars_near_listing (symbol absent / delisted / wrong symbol)"
    if RIGHTS.search(purpose or ""):
        return "needs_manual_review", "purpose mentions rights/scheme/demerger/merger/buyback - factor is not a clean ratio"
    if ev.get("isin_changed"):
        return "needs_manual_review", "ISIN changed around the listing (symbol change / demerger / scheme)"
    if ev.get("alias_hits"):
        tag = ("LIKELY SYMBOL RENAME (same-date match; the feed uses the current symbol, eod_raw the historical one)"
               if ev.get("alias_same_day") else "possible rename/alias or chance match")
        return "needs_manual_review", (f"{tag}: no gap under this symbol but a matching gap exists under: "
                                       + "; ".join(ev["alias_hits"]) + " - confirm with an NSE symbol-change map")
    if ev.get("flat_window"):
        return "reject_listing_or_data_mismatch", ("no price gap within +-%d sessions (stale/duplicate listing, "
                                                   "already-adjusted archive, or parser miss)" % WIDE)
    err, persist = ev.get("best_err", 9), ev.get("persistence_ratio", np.nan)
    persists = np.isfinite(persist) and abs(persist / factor - 1.0) <= TOL
    tol = TOL if (factor <= 0.8 or factor >= 1.25) else TOL_MILD        # same tolerances as the reconciler itself
    shift = ev.get("best_gap_offset_sessions", 99)
    if err <= tol and persists and ev.get("coverage") == "ok" and abs(shift) <= MAX_SHIFT:
        return "confirm_and_apply", (f"gap {ev['best_open_ratio']:.3f} matches listed {factor:.3f} at "
                                     f"{shift:+d} sessions from the listed ex-date and persists")
    if err <= TOL and abs(shift) > MAX_SHIFT:
        return "needs_manual_review", f"matching gap but {shift:+d} sessions from the listed ex-date (> {MAX_SHIFT})"
    if err <= TOL and ev.get("coverage") != "ok":
        return "needs_manual_review", "matching gap but too close to the edge of the data to check persistence"
    if err <= TOL and not persists:
        return "needs_manual_review", "gap matches the ratio but price did not stay at the new level (reversal)"
    return "needs_manual_review", (f"price gaps exist but none matches the listed factor "
                                   f"(closest open ratio {ev.get('best_open_ratio', float('nan')):.3f} vs {factor:.3f})")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args()

    from qsde.db.connection import read_sql
    from qsde.ingestion import nse_eod

    from qsde.ingestion.corp_actions_source import parse_purpose

    ev = read_sql("SELECT symbol, ex_date, source_purpose, snapped_ratio, ex_date_shift FROM corp_action_events "
                  "WHERE kind = 'SOURCE_UNCONFIRMED' ORDER BY symbol, ex_date")
    ev["listed_ex"] = pd.to_datetime(ev["ex_date"])
    ev["purpose"] = ev["source_purpose"]
    parsed = ev["purpose"].map(parse_purpose)
    ev["factor"] = parsed.map(lambda p: p["factor"])
    ev["detail"] = parsed.map(lambda p: "; ".join(x[2] for x in p["parts"]))
    ev = ev.drop_duplicates(["symbol", "listed_ex"]).reset_index(drop=True)

    syms = sorted(ev["symbol"].unique())
    raw = read_sql("SELECT exchange, symbol, series, date, isin, open, high, low, close, last, prev_close, volume, "
                   "turnover_lacs, trades, deliv_qty, deliv_per FROM eod_raw WHERE symbol = ANY(:s)", params={"s": syms})
    raw["date"] = pd.to_datetime(raw["date"])
    prim = nse_eod.pick_primary_series(raw)
    # market-wide slice around every listing, for the cross-symbol (alias) search
    parts = []
    for d in ev["listed_ex"].drop_duplicates():
        parts.append(read_sql("SELECT exchange, symbol, series, date, isin, open, close, prev_close, source_file FROM eod_raw "
                              "WHERE exchange = 'NSE' AND date BETWEEN CAST(:a AS DATE) AND CAST(:b AS DATE)",
                              params={"a": str((d - pd.Timedelta(days=22)).date()), "b": str((d + pd.Timedelta(days=22)).date())}))
    allraw = pd.concat(parts, ignore_index=True).drop_duplicates(["symbol", "series", "date"])
    allraw["date"] = pd.to_datetime(allraw["date"])
    prim_all = nse_eod.pick_primary_series(allraw)
    isin_map = {k: set(v) for k, v in raw.dropna(subset=['isin']).groupby('symbol')['isin'].unique().items()}
    isin_map.update({k: set(v) for k, v in allraw.dropna(subset=['isin']).groupby('symbol')['isin'].unique().items() if k not in isin_map})

    rows = []
    for r in ev.itertuples():
        factor = float(r.factor) if pd.notna(r.factor) else float("nan")
        purpose = r.purpose if isinstance(r.purpose, str) else (r.source_purpose or "")
        if not np.isfinite(factor):
            rows.append({"symbol": r.symbol, "listed_ex_date": r.listed_ex.date(), "purpose": purpose,
                         "bucket": "needs_manual_review", "reason": "factor could not be re-derived from the source row"})
            continue
        e = evidence(r, prim[prim["symbol"] == r.symbol], factor)
        if e.get("flat_window") or e.get("coverage") == "no_bars":
            tgt = prim[prim["symbol"] == r.symbol]
            e["alias_hits"] = alias_candidates(r.listed_ex, factor, r.symbol, prim_all, isin_map)
            e["alias_same_day"] = any(f"@{pd.Timestamp(r.listed_ex).date()}" in h for h in e["alias_hits"])
            ctx = tgt[(tgt["date"] >= pd.Timestamp(r.listed_ex) - pd.Timedelta(days=30)) &
                      (tgt["date"] <= pd.Timestamp(r.listed_ex) + pd.Timedelta(days=30))]
            e["raw_close_before"] = float(ctx[ctx["date"] < pd.Timestamp(r.listed_ex)]["close"].tail(1).mean()) if len(ctx) else np.nan
            e["raw_close_after"] = float(ctx[ctx["date"] >= pd.Timestamp(r.listed_ex)]["close"].head(1).mean()) if len(ctx) else np.nan
            e["source_files"] = "|".join(sorted(ctx["source_file"].astype(str).str[:7].unique())[:3]) if "source_file" in ctx else ""
        bucket, reason = classify(purpose, factor, e)
        rows.append({"symbol": r.symbol, "listed_ex_date": r.listed_ex.date(), "purpose": purpose,
                     "parsed": r.detail, "listed_factor": factor, "bucket": bucket, "reason": reason,
                     "record_date": "n/a (not in feed)", "face_value": "n/a (not in feed)", **e})

    df = pd.DataFrame(rows)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    df.to_csv(out / "source_unconfirmed_reconciliation.csv", index=False)
    counts = df["bucket"].value_counts().reindex(BUCKETS, fill_value=0)

    lines = ["# SOURCE_UNCONFIRMED reconciliation (read-only)", "",
             f"{len(df)} NSE-listed split/bonus/consolidation rows whose price gap did not confirm within +-2 sessions.",
             "Nothing was applied. `confirm_and_apply` is a PROPOSAL for owner review; the official `ohlcv` is unchanged.",
             "",
             "ISIN note: eod_raw carries an ISIN only for 2016 - mid 2020; for later listings ISIN continuity cannot be tested "
             "(the `isin_changed` flag is silent there, not a pass). Rejects were also searched across ALL symbols for a matching "
             "gap (renames/aliases): those are routed to needs_manual_review.",
             "",
             "Method note: the production reconciler needs the previous bar to be the immediately preceding market "
             "session. Illiquid names (BE/T-group series) skip sessions, so a real split can read as unconfirmed "
             "(e.g. DOLPHIN 2024-01-29: 1830.60 -> 186.70, no bar on the 25th). This report measures the gap against "
             "the last traded close instead. If you approve, the production rule should be widened to allow up to N "
             "skipped sessions (with the same tolerance and persistence checks), then `rebuild_from_db` rerun; "
             "that is a change to the official series and is NOT done here.",
             "", "| bucket | n |", "|---|---|"]
    lines += [f"| {b} | {int(counts[b])} |" for b in BUCKETS]
    for b in BUCKETS:
        sub = df[df["bucket"] == b]
        lines += ["", f"## {b} ({len(sub)})", "", "| symbol | listed ex | purpose | factor | evidence |", "|---|---|---|---|---|"]
        for r in sub.itertuples():
            lines.append(f"| {r.symbol} | {r.listed_ex_date} | {str(r.purpose)[:70]} | "
                         f"{getattr(r, 'listed_factor', float('nan')):.3f} | {r.reason} |")
    (out / "source_unconfirmed_reconciliation.md").write_text("\n".join(lines), encoding="utf-8")
    print(counts.to_string())
    print(f"report: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
