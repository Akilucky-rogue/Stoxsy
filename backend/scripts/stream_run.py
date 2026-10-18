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
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
log = logging.getLogger("stream_run")

STREAM_DIR = Path(r"D:\Stoxsy\data\stream")


def build(source: str, backfill_from, fetcher):
    from qsde.stream.adapters.nse import NseBhavcopyAdapter, NseCorporateActionsAdapter, NseSymbolChangeAdapter

    adapters = {
        "bhavcopy": NseBhavcopyAdapter(fetcher, start_date=backfill_from),
        "corporate_actions": NseCorporateActionsAdapter(fetcher),
        "symbol_changes": NseSymbolChangeAdapter(fetcher),
    }
    return list(adapters.values()) if source == "all" else [adapters[source]]


def sinks_for(kind: str):
    from qsde.stream.runner import InMemorySink
    from qsde.stream.sinks import PgCorporateActionsSink, PgEodRawSink, PgSymbolChangeSink

    if kind == "memory":
        mem = InMemorySink()
        return {"nse_bhavcopy": mem, "nse_corporate_actions": mem, "nse_symbol_changes": mem}
    return {"nse_bhavcopy": PgEodRawSink(), "nse_corporate_actions": PgCorporateActionsSink(),
            "nse_symbol_changes": PgSymbolChangeSink()}


class Router:
    """One sink per source behind the single `Sink` interface the runner expects."""

    def __init__(self, by_source):
        self.by_source = by_source

    def upsert(self, source, df, key_cols):
        return self.by_source[source].upsert(source, df, key_cols)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", choices=["bhavcopy", "corporate_actions", "symbol_changes", "all"], default="all")
    ap.add_argument("--mode", choices=["replay", "once", "loop"], default="replay")
    ap.add_argument("--sink", choices=["memory", "db"], default="memory")
    ap.add_argument("--enable-network", action="store_true", help="required for any mode that fetches")
    ap.add_argument("--enable-scheduled-downloads", action="store_true", help="required for --mode loop")
    ap.add_argument("--backfill-from", default=None, help="YYYY-MM-DD: first bhavcopy session to fetch (10 sessions per run)")
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
    cps = CheckpointStore(STREAM_DIR / "checkpoints.json")
    alerts = LogAlertSink(STREAM_DIR / "alerts.jsonl")
    bf = date.fromisoformat(args.backfill_from) if args.backfill_from else None
    adapters = build(args.source, bf, HttpFetcher() if args.enable_network else None)
    sink = Router(sinks_for(args.sink))
    mode = "backfill" if args.backfill_mode else "live"

    if args.mode == "replay":
        for a in adapters:
            r = replay(a, landing, sink, mode)
            print(f"replay {a.name}: ok={r.ok} items={r.items} rows_new={r.rows} errors={r.errors[:2]}")
        return 0
    if args.mode == "once":
        for a in adapters:
            r = run_once(a, landing, sink, health, cps, alerts, mode=mode)
            h = health.get(a.name)
            print(f"{a.name}: ok={r.ok} stage={r.stage} items={r.items} rows_new={r.rows} status={h.status} "
                  f"last_data={h.last_data_time} errors={r.errors[:1]}")
        return 0
    while True:                                             # mode == loop (owner switch)
        run_cycle(adapters, datetime.now(timezone.utc), landing, sink, health, cps, alerts)
        time.sleep(30)


if __name__ == "__main__":
    raise SystemExit(main())
