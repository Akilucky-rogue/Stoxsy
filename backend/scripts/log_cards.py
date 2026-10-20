"""Log today's defensive signal (and the decision-card sizes) into the immutable prediction ledger, in shadow mode.

    python scripts/log_cards.py --dry-run     # show what would be logged and the timing check
    python scripts/log_cards.py               # append (idempotent: identical re-logs are no-ops)
Run it after the end-of-day data load and `defensive_eval.py --latest`, BEFORE the next session opens: a prediction stamped after the next open is rejected
(the entry price would already be known). Nothing is traded or promoted.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
warnings.filterwarnings("ignore")

import pandas as pd


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    from api.routes import cockpit
    from qsde.db.connection import read_sql
    from qsde.decision.cards import PortfolioParams, build_cards
    from qsde.decision.transparency import UNIVERSE_METHOD
    from qsde.live_loop import ledger
    from qsde.live_loop.cards_log import HORIZON_IDS, portfolio_params_dict, realised_sigma, signal_predictions
    from qsde.ml import data as mldata
    from qsde.ml.registry import get_horizon
    from qsde.universe import instruments

    if not cockpit.SIGNAL.exists():
        print("no defensive signal computed (scripts/defensive_eval.py --latest)")
        return 1
    sc = pd.read_parquet(cockpit.SIGNAL)
    sc["date"] = pd.to_datetime(sc["date"])
    as_of = sc["date"].iloc[0]
    latest = pd.Timestamp(read_sql("SELECT max(date) AS d FROM ohlcv WHERE source LIKE 'nse_adj_%'")["d"].iloc[0])
    if as_of != latest:
        print(f"REFUSED: the signal is for {as_of.date()} but prices run to {latest.date()}; recompute the signal first")
        return 1
    now = pd.Timestamp.now(tz="UTC")
    deadline = ledger.entry_deadline_ist(as_of)
    print(f"signal {as_of.date()} ({len(sc)} names); now {now.tz_convert('Asia/Kolkata'):%Y-%m-%d %H:%M} IST; log by {deadline:%Y-%m-%d %H:%M} IST")
    start = str((as_of - pd.Timedelta(days=700)).date())
    px = mldata.load_prices(sc["symbol"].tolist(), start=start)
    cl = px[["symbol", "date", "close"]]
    last_close = cl.sort_values("date").groupby("symbol")["close"].last()
    sigma, weights = {}, {}
    for hid in HORIZON_IDS:
        spec = get_horizon(hid)
        p = PortfolioParams(horizon=hid)
        cand = sc[(sc["risk_bucket"] >= p.min_bucket) & (sc["liq"] >= p.min_traded_value)]["symbol"]
        book = build_cards(sc, cl[cl["symbol"].isin(cand)], p)
        weights[hid] = {c["symbol"]: c["weight"] for c in book["cards"]}
        sigma[hid] = pd.Series({s: realised_sigma(g.sort_values("date")["close"], spec.barrier.vol_span) for s, g in cl.groupby("symbol")})
        print(f"  {hid}: {len(book['cards'])} cards, invested {book['summary']['invested_pct']:.0%}")
    from qsde.ingestion import ca_series
    try:
        sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, cwd=Path(__file__).resolve().parents[2]).stdout.strip() or None
    except OSError:
        sha = None
    lineage = {"universe_version": f"{UNIVERSE_METHOD}|{instruments.tag()}", "ca_source_hash": ca_series.lineage_tag(), "git_sha": sha,
               "model_card_id": "defensive_signal_v1"}
    df = signal_predictions(sc, last_close, sigma, weights, now, portfolio_params_dict(PortfolioParams()), lineage)
    print(f"  {len(df)} predictions ({df['horizon_id'].nunique()} horizons)")
    if args.dry_run:
        try:
            ledger.prepare(df, now=now, enforce_pre_entry=True)
            print("dry run: passes the point-in-time and pre-entry checks")
        except ledger.PITViolation as e:
            print(f"dry run: WOULD BE REJECTED: {e}")
            return 2
        return 0
    try:
        print(ledger.append_predictions(df, now=now))
    except ledger.PITViolation as e:
        print(f"REJECTED: {e}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
