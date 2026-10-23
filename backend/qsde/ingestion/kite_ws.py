"""Kite market-data WebSocket client on the `websockets` library, a drop-in for the part of `kiteconnect.KiteTicker` that Stoxsy uses.

Why: the SDK's transport (autobahn 19.11 on Twisted) is refused by Kite's server on current Python ("WebSocket connection upgrade failed (400 - BadRequest)") while the
same account and token connect fine with a standards-compliant client. Only the transport is replaced: packets are parsed with the SDK's own binary parser, so tick fields are
identical to what the rest of the code already expects. Read-only market data: this client has no order functionality.

Interface kept: attributes on_ticks / on_connect / on_close / on_error / on_reconnect / on_noreconnect, MODE_LTP / MODE_QUOTE / MODE_FULL,
connect(threaded=True), subscribe(tokens), set_mode(mode, tokens), close(). Reconnects with capped exponential backoff; subscriptions are re-sent by the
caller's on_connect (as with the SDK).
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
from typing import Callable, Optional

import websockets
from kiteconnect import KiteTicker

log = logging.getLogger(__name__)
ROOT = "wss://ws.kite.trade"


class KiteWebSocket:
    MODE_LTP, MODE_QUOTE, MODE_FULL = "ltp", "quote", "full"

    def __init__(self, api_key: str, access_token: str, root: str = ROOT, reconnect: bool = True, max_delay: float = 60.0, max_tries: int = 300, open_timeout: float = 30.0):
        self._api_key, self._token, self._root = api_key, access_token, root
        self._reconnect, self._max_delay, self._max_tries, self._open_timeout = reconnect, max_delay, max_tries, open_timeout
        self.on_ticks: Optional[Callable] = None
        self.on_connect: Optional[Callable] = None
        self.on_close: Optional[Callable] = None
        self.on_error: Optional[Callable] = None
        self.on_reconnect: Optional[Callable] = None
        self.on_noreconnect: Optional[Callable] = None
        self._parser = KiteTicker("parser", "parser")          # used only for its binary packet parser; it never connects
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._ws = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.last_message_at: Optional[float] = None

    # ── lifecycle ────────────────────────────────────────────────

    def connect(self, threaded: bool = True) -> None:
        if threaded:
            self._thread = threading.Thread(target=self._run, name="kite-ws", daemon=True)
            self._thread.start()
        else:
            self._run()

    def close(self) -> None:
        self._stop.set()
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self._shutdown(), self._loop)

    async def _shutdown(self) -> None:
        if self._ws is not None:
            await self._ws.close()

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._main())
        finally:
            self._loop.close()

    async def _main(self) -> None:
        import time

        attempts = 0
        url = f"{self._root}/?api_key={self._api_key}&access_token={self._token}"
        while not self._stop.is_set():
            try:
                async with websockets.connect(url, open_timeout=self._open_timeout, ping_interval=None, max_size=None) as ws:
                    self._ws = ws
                    attempts = 0
                    self._call(self.on_connect, self, {})
                    async for msg in ws:
                        self.last_message_at = time.time()
                        if isinstance(msg, bytes):
                            if len(msg) > 4 and self.on_ticks:
                                ticks = self._parser._parse_binary(msg)
                                if ticks:
                                    self._call(self.on_ticks, self, ticks)
                        else:
                            self._text(msg)
                self._call(self.on_close, self, 1000, "connection closed")
            except Exception as e:                                       # noqa: BLE001 - every transport failure is reported, then retried
                code = getattr(getattr(e, "rcvd", None), "code", 1006)
                self._call(self.on_error, self, code, f"{type(e).__name__}: {str(e)[:200]}")
                self._call(self.on_close, self, code, str(e)[:200])
            finally:
                self._ws = None
            if self._stop.is_set() or not self._reconnect:
                break
            attempts += 1
            if attempts > self._max_tries:
                self._call(self.on_noreconnect, self)
                break
            delay = min(self._max_delay, 2 ** min(attempts, 6))
            self._call(self.on_reconnect, self, attempts)
            await asyncio.sleep(delay)

    def _text(self, payload: str) -> None:
        try:
            d = json.loads(payload)
        except ValueError:
            return
        if d.get("type") == "error":
            self._call(self.on_error, self, 0, d.get("data"))

    def _call(self, fn: Optional[Callable], *args) -> None:
        if fn is None:
            return
        try:
            fn(*args)
        except Exception:                                                # noqa: BLE001 - a consumer bug must not kill the socket
            log.exception("callback %s failed", getattr(fn, "__name__", fn))

    # ── commands (thread-safe) ───────────────────────────────────

    def _send(self, obj: dict) -> None:
        if not (self._loop and self._ws):
            raise RuntimeError("not connected")
        try:
            on_loop = asyncio.get_running_loop() is self._loop
        except RuntimeError:
            on_loop = False
        if on_loop:                                   # called from a callback on the socket's own thread (e.g. on_connect): queue, never wait
            self._loop.create_task(self._ws.send(json.dumps(obj)))
            return
        asyncio.run_coroutine_threadsafe(self._ws.send(json.dumps(obj)), self._loop).result(timeout=10)

    def subscribe(self, tokens: list[int]) -> None:
        self._send({"a": "subscribe", "v": list(tokens)})

    def unsubscribe(self, tokens: list[int]) -> None:
        self._send({"a": "unsubscribe", "v": list(tokens)})

    def set_mode(self, mode: str, tokens: list[int]) -> None:
        self._send({"a": "mode", "v": [mode, list(tokens)]})
