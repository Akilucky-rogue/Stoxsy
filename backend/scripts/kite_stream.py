"""
Live Kite WebSocket streamer daemon.

Long-running process that:
  1. Loads the active access_token from kite_tokens.
  2. Resolves the universe's instrument_tokens via kite_instruments.
  3. Opens a WebSocket subscription to all universe tokens (MODE_QUOTE).
  4. Spawns the MinuteBarAggregator + PeriodicFlusher threads to consume
     ticks and emit minute bars + raw tick log.
  5. Sleeps the main thread; clean Ctrl-C shutdown.

Usage:
    # Stream the full active universe
    python scripts/kite_stream.py

    # Stream only a few symbols (testing)
    python scripts/kite_stream.py --symbols RELIANCE,TCS,INFY

    # Don't persist raw ticks (lighter footprint)
    python scripts/kite_stream.py --no-raw-ticks

Recommended deployment:
  * Windows Task Scheduler: trigger at 09:10 IST weekdays, stop at 15:35.
    NSE market hours are 09:15-15:30. Connecting earlier reserves the
    subscription; ticks only arrive once trading opens.
  * Or just run it in a dedicated PowerShell window during market hours.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qsde.ops.heartbeat import beat
from qsde.db.connection import read_sql
from qsde.ingestion.kite_ticker import KiteLiveStreamer
from qsde.ingestion.intraday_storage import MinuteBarAggregator, PeriodicFlusher

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(threadName)s] %(message)s",
)
log = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", type=str, default=None,
                        help="Comma-separated symbols (default: the point-in-time top-500 universe)")
    parser.add_argument("--no-raw-ticks", action="store_true",
                        help="Skip writing raw ticks to ticks_raw")
    parser.add_argument("--debug", action="store_true",
                        help="Enable DEBUG logging (per-batch tick trace from kite_ticker).")
    parser.add_argument("--heartbeat-every", type=int, default=None,
                        help="Override QSDE_TICK_HEARTBEAT (log INFO every N ticks).")
    args = parser.parse_args()

    if args.debug:
        # Root logger -> DEBUG so the new per-batch line in kite_ticker fires.
        logging.getLogger().setLevel(logging.DEBUG)
        log.info("DEBUG logging enabled.")
    if args.heartbeat_every is not None:
        import os
        os.environ["QSDE_TICK_HEARTBEAT"] = str(args.heartbeat_every)
        log.info("Tick heartbeat every %d ticks.", args.heartbeat_every)

    # 1. Resolve target instrument_tokens.
    if args.symbols:
        syms = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
        rows = read_sql(
            "SELECT instrument_token, tradingsymbol FROM kite_instruments "
            "WHERE tradingsymbol = ANY(:syms) AND instrument_type='EQ' AND exchange='NSE'",
            params={"syms": syms},
        )
    else:
        # Default: the point-in-time top-500 universe (the same names the signal and the logged predictions cover), minus ETFs and fund units.
        from qsde.universe import instruments

        drop = sorted(instruments.non_equity_symbols())
        rows = read_sql(
            """SELECT ki.instrument_token, ki.tradingsymbol
                 FROM universe_pit up
                 JOIN kite_instruments ki ON ki.tradingsymbol = up.symbol AND ki.instrument_type = 'EQ' AND ki.exchange = 'NSE'
                WHERE up.method = 'adv60_top500_v1' AND up.date = (SELECT max(date) FROM universe_pit WHERE method = 'adv60_top500_v1')
                  AND NOT (up.symbol = ANY(:drop))
             ORDER BY ki.tradingsymbol""", params={"drop": drop}
        )
        log.info("Streaming universe: point-in-time top 500 (%d resolved to Kite tokens).", len(rows))

    if rows.empty:
        log.error("No instrument_tokens resolved. Run /api/kite/refresh_instruments first.")
        sys.exit(1)

    tokens = rows["instrument_token"].astype(int).tolist()
    log.info("Resolved %d instrument_tokens.", len(tokens))

    # 2. Wire and start the pipeline.
    aggregator = MinuteBarAggregator(log_raw_ticks=not args.no_raw_ticks)
    flusher    = PeriodicFlusher(aggregator, interval_sec=30.0)
    streamer   = KiteLiveStreamer(instrument_tokens=tokens, mode="quote")

    aggregator.start()
    flusher.start()
    streamer.start()
    log.info("Kite live stream up. Press Ctrl-C to stop.")

    # 3. Graceful shutdown on SIGINT/SIGTERM.
    stop_received = {"flag": False}
    def _on_signal(sig, frame):
        if stop_received["flag"]:
            log.warning("Force-quitting.")
            sys.exit(1)
        stop_received["flag"] = True
        log.info("Signal %s received; shutting down...", sig)
        streamer.stop()
        flusher.stop()
        aggregator.stop()

    signal.signal(signal.SIGINT, _on_signal)
    try:
        signal.signal(signal.SIGTERM, _on_signal)
    except (AttributeError, ValueError):
        pass  # Windows may not have SIGTERM in some PS contexts

    # Main thread parks here -- everything else is in background threads.
    try:
        last_beat = 0.0
        while not stop_received["flag"]:
            time.sleep(2.0)
            if time.time() - last_beat >= 30:                      # liveness for the alert engine and the doctor command
                h = streamer.health()
                beat("stream", "ok" if h["connected"] else "degraded", h)
                last_beat = time.time()
    finally:
        beat("stream", "stopped", streamer.health())
        log.info("Joining threads...")
        flusher.join(timeout=5)
        aggregator.join(timeout=10)
        log.info("Exited cleanly.")


if __name__ == "__main__":
    main()
