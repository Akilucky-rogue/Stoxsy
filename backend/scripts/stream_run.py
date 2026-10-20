"""Run the NSE EOD sources on the streaming foundation. SAFE BY DEFAULT: nothing touches the network unless asked.

    python scripts/stream_run.py --source all --mode replay                    # offline: re-parse landed raw files into a sink
    python scripts/stream_run.py --source corporate_actions --mode once --enable-network --sink db
    python scripts/stream_run.py --source bhavcopy --mode once --enable-network --backfill-from 2026-05-01 --sink db
    python scripts/stream_run.py --source all --mode loop --enable-network --enable-scheduled-downloads --sink db   # NOT enabled yet

Landing zone: D:\\Stoxsy\\data\\landing (raw bytes, immutable). Health: D:\\Stoxsy\\data\\stream\\health.json (+ source_health table
with --sink db). Alerts: D:\\Stoxsy\\data\\stream\\alerts.jsonl. Scheduled downloads are a separate, explicit switch: the owner turns them
on after replay and failure-injection tests pass.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import date, timedelta, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
log = logging.getLogger("stream_run")

STREAM_DIR = Path(r"D:\Stoxsy\data\stream")


def landed_sessions(landing, source: str = "nse_bhavcopy") -> set:
    """Bhavcopy sessions already in the landing zone (their sidecars carry the session date): makes a backfill resumable."""
    out = set()
    for rec in landing.records(source):
        if "session_date" in rec.meta:
            out.add(date.fromisoformat(rec.meta["session_date"]))
    return out


def _ca_adapter(fetcher, backfill_from, landing):
    from qsde.stream.adapters.nse import NseCorporateActionsAdapter

    if backfill_from is None:
        return NseCorporateActionsAdapter(fetcher)
    done = set()
    if landing is not None:
        for rec in landing.records("nse_corporate_actions"):
            if rec.meta.get("backfill") and "from" in rec.meta:
                done.add((date.fromisoformat(rec.meta["from"]), date.fromisoformat(rec.meta["to"])))
    return NseCorporateActionsAdapter(fetcher, windows=NseCorporateActionsAdapter.quarter_windows(backfill_from, date.today()),
                                      landed_windows=done)


def amfi_windows(backfill_from, monthly_from=None):
    """Quarter windows, switching to monthly windows from `monthly_from` (the portal hangs or answers with an error page on large windows)."""
    from qsde.stream.adapters.amfi import AmfiNavAdapter

    if not backfill_from:
        return None
    if monthly_from is None or monthly_from <= backfill_from:
        return AmfiNavAdapter.quarter_windows(backfill_from, date.today(), months=1 if monthly_from else 3)
    q = AmfiNavAdapter.quarter_windows(backfill_from, monthly_from - timedelta(days=1))
    return q + AmfiNavAdapter.quarter_windows(monthly_from, date.today(), months=1)


def build(source: str, backfill_from, fetcher, landing=None, backfill_to=None, extra_dates=(), monthly_from=None):
    from qsde.stream.adapters.nse import NseBhavcopyAdapter, NseCorporateActionsAdapter, NseSymbolChangeAdapter

    from qsde.stream.adapters.amfi import AmfiNavAdapter
    from qsde.stream.adapters.bse import BseBhavcopyAdapter
    from qsde.stream.adapters.nse_fo import NseFoBhavcopyAdapter

    amfi_done = set()
    if landing is not None:
        for rec in landing.records("amfi_nav"):
            if rec.meta.get("kind") == "history" and "from" in rec.meta and rec.path.stat().st_size > 100_000:      # a small file is a portal error page, not data
                amfi_done.add((date.fromisoformat(rec.meta["from"]), date.fromisoformat(rec.meta["to"])))
    adapters = {
        "amfi_nav": AmfiNavAdapter(fetcher, windows=amfi_windows(backfill_from, monthly_from), landed_windows=amfi_done),
        "amfi_navall": AmfiNavAdapter(fetcher, navall=True),
        "nse_fo_bhavcopy": NseFoBhavcopyAdapter(fetcher, start_date=backfill_from, end_date=backfill_to, extra_dates=extra_dates,
                                                landed_sessions=landed_sessions(landing, "nse_fo_bhavcopy") if landing is not None else ()),
        "bse_bhavcopy": BseBhavcopyAdapter(fetcher, start_date=backfill_from, end_date=backfill_to, extra_dates=extra_dates,
                                           landed_sessions=landed_sessions(landing, "bse_bhavcopy") if landing is not None else ()),
        "bhavcopy": NseBhavcopyAdapter(fetcher, start_date=backfill_from,
                                       landed_sessions=landed_sessions(landing) if landing is not None else ()),
        "corporate_actions": _ca_adapter(fetcher, backfill_from, landing),
        "symbol_changes": NseSymbolChangeAdapter(fetcher),
    }
    # "all" is the NSE bundle (the owner approved BSE separately): BSE runs only when asked for by name
    return [a for k, a in adapters.items() if k not in ("bse_bhavcopy", "amfi_nav", "amfi_navall", "nse_fo_bhavcopy")] if source == "all" else [adapters[source]]


def sinks_for(kind: str):
    from qsde.stream.runner import InMemorySink
    from qsde.stream.sinks import PgBseEodSink, PgCorporateActionsSink, PgFnoEodSink, PgMfNavSink, PgEodRawSink, PgSymbolChangeSink

    if kind == "memory":
        mem = InMemorySink()
        return {"nse_bhavcopy": mem, "nse_corporate_actions": mem, "nse_symbol_changes": mem, "bse_bhavcopy": mem, "amfi_nav": mem, "nse_fo_bhavcopy": mem}
    return {"nse_bhavcopy": PgEodRawSink(), "nse_corporate_actions": PgCorporateActionsSink(),
            "nse_symbol_changes": PgSymbolChangeSink(), "bse_bhavcopy": PgBseEodSink(), "amfi_nav": PgMfNavSink(), "nse_fo_bhavcopy": PgFnoEodSink()}


class Router:
    """One sink per source behind the single `Sink` interface the runner expects."""

    def __init__(self, by_source):
        self.by_source = by_source

    def upsert(self, source, df, key_cols):
        return self.by_source[source].upsert(source, df, key_cols)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", choices=["bhavcopy", "corporate_actions", "symbol_changes", "bse_bhavcopy", "nse_fo_bhavcopy", "amfi_nav", "amfi_navall", "all"], default="all")
    ap.add_argument("--mode", choices=["replay", "once", "loop"], default="replay")
    ap.add_argument("--sink", choices=["memory", "db"], default="memory")
    ap.add_argument("--enable-network", action="store_true", help="required for any mode that fetches")
    ap.add_argument("--enable-scheduled-downloads", action="store_true", help="required for --mode loop")
    ap.add_argument("--backfill-from", default=None, help="YYYY-MM-DD: first bhavcopy session to fetch (10 sessions per run)")
    ap.add_argument("--monthly-from", default=None, help="YYYY-MM-DD: AMFI backfill uses monthly windows from this date")
    ap.add_argument("--backfill-to", default=None, help="YYYY-MM-DD: last session of a BSE backfill segment (parallel workers)")
    ap.add_argument("--extra-dates", default=None, help="comma-separated YYYY-MM-DD sessions to fetch regardless of weekday (BSE)")
    ap.add_argument("--checkpoint-file", default="checkpoints.json", help="parallel workers each need their own checkpoint file")
    ap.add_argument("--backfill-mode", action="store_true", help="stamp known_at as publication time, not fetch time (historical data)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.mode in ("once", "loop") and not args.enable_network:
        log.error("mode %s fetches from NSE; pass --enable-network to allow it", args.mode)
        return 2
    if args.mode == "loop" and not args.enable_scheduled_downloads:
        log.error("scheduled downloads are not enabled; pass --enable-scheduled-downloads (owner switch)")
        return 2

    from qsde.stream.adapters.nse import HttpFetcher
    from qsde.stream.health import CheckpointStore, JsonHealthStore, LogAlertSink, PgHealthStore
    from qsde.stream.landing import LandingZone
    from qsde.stream.runner import replay, run_once
    from qsde.stream.scheduler import run_cycle

    STREAM_DIR.mkdir(parents=True, exist_ok=True)
    landing = LandingZone()
    health = PgHealthStore() if args.sink == "db" else JsonHealthStore(STREAM_DIR / "health.json")
    cps = CheckpointStore(STREAM_DIR / args.checkpoint_file)
    alerts = LogAlertSink(STREAM_DIR / "alerts.jsonl")
    bf = date.fromisoformat(args.backfill_from) if args.backfill_from else None
    bt = date.fromisoformat(args.backfill_to) if args.backfill_to else None
    adapters = build(args.source, bf, HttpFetcher() if args.enable_network else None, landing, bt, tuple(date.fromisoformat(x) for x in args.extra_dates.split(',')) if args.extra_dates else (),
                     date.fromisoformat(args.monthly_from) if args.monthly_from else None)
    sink = Router(sinks_for(args.sink))
    mode = "backfill" if args.backfill_mode else "live"

    if args.mode == "replay":
        for a in adapters:
            r = replay(a, landing, sink, mode)
            print(f"replay {a.name}: ok={r.ok} items={r.items} rows_new={r.rows} errors={r.errors[:2]}")
        return 0
    if args.mode == "once":
        for a in adapters:
            total_items = total_rows = 0
            for _ in range(60 if (bf and a.name in ("nse_bhavcopy", "nse_corporate_actions", "bse_bhavcopy", "amfi_nav", "nse_fo_bhavcopy")) else 1):       # backfill: keep going until nothing new
                r = run_once(a, landing, sink, health, cps, alerts, mode=mode)
                total_items += r.items
                total_rows += r.rows
                if not r.ok or r.items == 0:
                    break
            h = health.get(a.name)
            print(f"{a.name}: ok={r.ok} stage={r.stage} sessions_or_files={total_items} rows_new={total_rows} status={h.status} "
                  f"last_data={h.last_data_time} errors={r.errors[:1]}")
        return 0
    while True:                                             # mode == loop (owner switch)
        run_cycle(adapters, datetime.now(timezone.utc), landing, sink, health, cps, alerts)
        time.sleep(30)


if __name__ == "__main__":
    raise SystemExit(main())
