"""Kite WebSocket client against a local fake server (no network, no account): connect, subscribe from on_connect (no deadlock), parse LTP and quote packets,
text errors, reconnect after a dropped socket, clean close."""
import asyncio
import json
import struct
import threading
import time

import pytest
import websockets

from qsde.ingestion.kite_ws import KiteWebSocket

TOKEN = (2885 << 8) | 1                               # NSE segment id 1 in the low byte: token = instrument_id * 256 + 1


def ltp_packet(tok, price_paise):
    body = struct.pack(">II", tok, price_paise)
    return struct.pack(">H", 1) + struct.pack(">H", len(body)) + body


class Server:
    def __init__(self):
        self.received, self.connections, self.drop_first = [], 0, False
        self.loop, self.started = None, threading.Event()

    async def handler(self, ws):
        self.connections += 1
        n = self.connections
        await ws.send(json.dumps({"type": "instruments_meta", "data": {"count": 0}}))
        async for m in ws:
            self.received.append(json.loads(m))
            if self.received[-1]["a"] == "subscribe":
                await ws.send(ltp_packet(TOKEN, 123456))
                await ws.send(json.dumps({"type": "error", "data": "demo error"}))
                if self.drop_first and n == 1:
                    await ws.close()
                    return

    def run(self):
        async def main():
            async with websockets.serve(self.handler, "127.0.0.1", 0) as srv:
                self.port = srv.sockets[0].getsockname()[1]
                self.loop = asyncio.get_running_loop()
                self.started.set()
                self.stop = asyncio.Event()
                await self.stop.wait()
        asyncio.run(main())


@pytest.fixture()
def server():
    s = Server()
    t = threading.Thread(target=s.run, daemon=True)
    t.start()
    assert s.started.wait(5)
    yield s
    s.loop.call_soon_threadsafe(s.stop.set)


def wait(cond, secs=6):
    end = time.time() + secs
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def client(server, **kw):
    c = KiteWebSocket("key", "tok", root=f"ws://127.0.0.1:{server.port}", **kw)
    c.ticks, c.errors, c.events = [], [], []
    c.on_ticks = lambda ws, t: c.ticks.extend(t)
    c.on_error = lambda ws, code, reason: c.errors.append((code, reason))
    c.on_connect = lambda ws, r: (c.events.append("connect"), ws.subscribe([TOKEN]), ws.set_mode(ws.MODE_LTP, [TOKEN]))
    c.on_reconnect = lambda ws, n: c.events.append(f"reconnect{n}")
    return c


def test_subscribes_from_on_connect_parses_ltp_and_reports_text_errors(server):
    c = client(server)
    c.connect(threaded=True)
    try:
        assert wait(lambda: len(c.ticks) >= 1), "no tick received (on_connect subscribe may have deadlocked)"
        assert c.ticks[0]["instrument_token"] == TOKEN and c.ticks[0]["last_price"] == pytest.approx(1234.56) and c.ticks[0]["mode"] == "ltp"
        assert wait(lambda: any(r == "demo error" for _, r in c.errors))
        assert [m["a"] for m in server.received][:2] == ["subscribe", "mode"] and server.received[1]["v"] == ["ltp", [TOKEN]]
    finally:
        c.close()


def test_reconnects_after_the_server_drops_the_socket_and_resubscribes(server):
    server.drop_first = True
    c = client(server, max_delay=1.0)
    c.connect(threaded=True)
    try:
        assert wait(lambda: server.connections >= 2, 15), "did not reconnect"
        assert wait(lambda: len(c.ticks) >= 2, 10) and c.events.count("connect") >= 2 and any(e.startswith("reconnect") for e in c.events)
        assert [m["a"] for m in server.received].count("subscribe") >= 2
    finally:
        c.close()


def test_close_stops_the_thread_without_reconnecting(server):
    c = client(server)
    c.connect(threaded=True)
    assert wait(lambda: len(c.ticks) >= 1)
    c.close()
    assert wait(lambda: not c._thread.is_alive(), 8)
    n = server.connections
    time.sleep(1.5)
    assert server.connections == n


def test_a_failing_callback_does_not_kill_the_socket(server):
    c = client(server)
    got = []
    c.on_ticks = lambda ws, t: (got.append(1), (_ for _ in ()).throw(RuntimeError("consumer bug")))
    c.connect(threaded=True)
    try:
        assert wait(lambda: len(got) >= 1)
        assert c._thread.is_alive()
    finally:
        c.close()


def test_a_refused_connection_is_reported_and_retried_not_raised():
    c = KiteWebSocket("key", "tok", root="ws://127.0.0.1:9", max_delay=1.0, open_timeout=2)
    errs = []
    c.on_error = lambda ws, code, reason: errs.append(reason)
    c.connect(threaded=True)
    try:
        assert wait(lambda: len(errs) >= 1, 8)
    finally:
        c.close()
