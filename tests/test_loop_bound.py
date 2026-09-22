"""A poller's cached client must never be reused on another thread's event loop.

The desktop runs each platform's poller on its own thread + loop, and the
dashboard ("Check sessions now", "Poll now", connect) on uvicorn's. Reusing the
cached client's pooled keep-alive connection from the second loop failed with
"<asyncio.locks.Event …> is bound to a different event loop". See
``polling/loop_bound.py``.
"""
import asyncio
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from polling import ig_poller


class _KeepAlive(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"          # keep-alive, so the pool reuses the socket

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *a):
        pass


def _loop_thread():
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    return loop


@pytest.fixture
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _KeepAlive)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}/"
    srv.shutdown()


def test_second_loop_gets_its_own_client_and_its_request_succeeds(server, monkeypatch):
    monkeypatch.setattr(ig_poller, "_ig_client", None)

    async def fetch():
        c = ig_poller._get_or_create_client({}, "tok", "")
        return c, (await c._http.get(server)).status_code

    poller, dashboard = _loop_thread(), _loop_thread()
    run = lambda loop: asyncio.run_coroutine_threadsafe(fetch(), loop).result(10)
    a, status_a = run(poller)
    a_again, _ = run(poller)
    b, status_b = run(dashboard)       # raised "bound to a different event loop" before
    assert (status_a, status_b) == (200, 200)
    assert a_again is a                # same loop: the cached client (and its session) is kept
    assert b is not a


def test_every_cached_poller_client_goes_through_loop_bound():
    root = Path(__file__).resolve().parent.parent / "polling"
    getters = [p for p in root.glob("*_poller.py") if "def _get_or_create_client" in p.read_text("utf-8")]
    assert len(getters) >= 19
    missing = [p.name for p in getters
               if "loop_bound.reusable(" not in p.read_text("utf-8")
               or "return loop_bound.pin(" not in p.read_text("utf-8")]
    assert not missing, missing
