"""Behind Tailscale serve/funnel or Caddy, a visitor must never look like localhost.

Loopback is the gate for first-run password setup, the sensitive-when-open endpoints and
/api/server/update-claim (dashboard.py::_client_is_loopback). A same-host proxy connects
from 127.0.0.1, so the gate only holds because uvicorn swaps in X-Forwarded-For from a
trusted proxy — and trusts ONLY 127.0.0.1. Tailscale's proxy SETS that header to the real
source (ipn/ipnlocal/serve.go addProxyForwardedHeaders; a funnel visitor's public IP), so
a visitor can't forge it. Widening the trust to "*" would let anyone who reaches the port
claim loopback; dropping proxy_headers would make every proxied visitor loopback.
"""
import threading
import time
import urllib.request
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request

import config
from dashboard import _client_is_loopback


def test_server_trusts_forwarded_headers_from_localhost_only():
    assert config.DASHBOARD_FORWARDED_IPS == "127.0.0.1"
    src = (Path(__file__).resolve().parent.parent / "server.py").read_text("utf-8")
    assert "proxy_headers=True" in src and "forwarded_allow_ips=config.DASHBOARD_FORWARDED_IPS" in src


def test_a_proxied_visitor_is_not_loopback():
    app = FastAPI()

    @app.get("/who")
    def who(request: Request):
        return {"loopback": _client_is_loopback(request)}

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning",
                                           proxy_headers=True,
                                           forwarded_allow_ips=config.DASHBOARD_FORWARDED_IPS))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    try:
        deadline = time.time() + 10
        while not server.started and time.time() < deadline:
            time.sleep(0.05)
        port = server.servers[0].sockets[0].getsockname()[1]

        def loopback(headers):
            req = urllib.request.Request(f"http://127.0.0.1:{port}/who", headers=headers)
            return urllib.request.urlopen(req).read() == b'{"loopback":true}'

        # Exactly what Tailscale's proxy adds for a funnel visitor, and for a tailnet device.
        assert not loopback({"X-Forwarded-For": "203.0.113.9", "X-Forwarded-Proto": "https",
                             "Tailscale-Funnel-Request": "?1"})
        assert not loopback({"X-Forwarded-For": "100.101.102.103", "X-Forwarded-Proto": "https"})
        assert loopback({})              # the machine itself, straight at the port
    finally:
        server.should_exit = True
        t.join(5)


def test_the_docker_line_in_the_guide_sees_the_real_visitor_and_no_one_else():
    """docs/SETUP.md tells Docker users behind a tunnel to trust loopback + Docker's bridge range.

    A host-side proxy (cloudflared, Caddy, tailscale serve) reaches the container through
    docker-proxy, so the peer is the bridge gateway (172.x.0.1), never 127.0.0.1. Cloudflare
    APPENDS the visitor's IP to any X-Forwarded-For the visitor sent, so the leftmost entry is
    forgeable; uvicorn with "*" takes exactly that one.
    """
    import asyncio
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    line = "PAWPOLLER_FORWARDED_IPS=127.0.0.1,172.16.0.0/12"
    guide = (Path(__file__).resolve().parent.parent / "docs" / "SETUP.md").read_text("utf-8")
    assert line in guide
    trusted = line.split("=", 1)[1]

    def seen(peer, trust, xff="127.0.0.1, 203.0.113.9"):
        out = {}

        async def app(scope, receive, send):
            out.update(host=scope["client"][0], scheme=scope["scheme"])

        scope = {"type": "http", "scheme": "http", "client": (peer, 40000),
                 "headers": [(b"x-forwarded-for", xff.encode()), (b"x-forwarded-proto", b"https")]}
        asyncio.run(ProxyHeadersMiddleware(app, trusted_hosts=trust)(scope, None, None))
        return out

    assert seen("172.18.0.1", trusted) == {"host": "203.0.113.9", "scheme": "https"}
    assert seen("203.0.113.5", trusted)["host"] == "203.0.113.5"  # a stranger's header is ignored
    assert seen("172.18.0.1", "*")["host"] == "127.0.0.1"  # why the guide says never "*"


def test_nothing_in_the_repo_still_recommends_trusting_everything():
    """4.32.3: config.py's own comment told self-hosters to set `*` — the setting the
    guide forbids, and the one that lets a visitor claim loopback. The file a person
    edits their .env next to must not contradict the guide."""
    src = (Path(__file__).resolve().parent.parent / "config.py").read_text("utf-8")
    block = src[src.index("# Trusted proxy IPs"):src.index("DASHBOARD_FORWARDED_IPS =")]
    assert "PAWPOLLER_FORWARDED_IPS=*" not in block
    assert "NEVER `*`" in block
    assert "127.0.0.1,172.16.0.0/12" in block      # the value SETUP.md gives


def test_the_ig_relay_counts_the_address_uvicorn_resolved():
    """The relay's per-address cap read the raw X-Forwarded-For, so rotating a header
    walked straight past it whatever PAWPOLLER_FORWARDED_IPS said."""
    src = (Path(__file__).resolve().parent.parent / "routes" / "ig_api.py").read_text("utf-8")
    body = src[src.index("def _client_ip("):src.index("def _relay_rate_ok(")]
    assert 'headers.get("x-forwarded-for"' not in body.lower()   # read, not merely mentioned
    assert "request.client.host" in body
