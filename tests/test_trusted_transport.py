"""Pairing over Tailscale (HOSTFREE, 4.11.0).

A bearer token may travel over HTTPS anywhere, plain HTTP to the loopback, or plain
HTTP over Tailscale (100.64.0.0/10, fd7a:115c:a1e0::/48, *.ts.net) — never plain
HTTP to anything else. One helper, used by auto_sync's push path and the wizard's
pair-test, so the two can never disagree again.
"""
from __future__ import annotations

import pytest

import auto_sync
import config
from routes import settings_api


@pytest.mark.parametrize("url,ok,reason", [
    ("https://pawpoller.example", True, "https"),
    ("https://box.tail1234.ts.net", True, "https"),
    ("http://localhost:8420", True, "loopback"),
    ("http://127.0.0.1:8420", True, "loopback"),
    ("http://127.5.5.5", True, "loopback"),
    ("http://box.tail1234.ts.net:8420", True, "tailscale name"),
    ("http://100.101.102.103:8420", True, "tailscale address"),
    ("http://[fd7a:115c:a1e0::1]:8420", True, "tailscale address"),
    ("http://192.168.1.20:8420", False, "plain http"),
    ("http://100.63.255.255", False, "plain http"),          # just outside the CGNAT range
    ("http://100.128.0.1", False, "plain http"),             # just past it
    ("http://ts.net.example.com", False, "plain http"),      # a look-alike, not *.ts.net
    ("http://pawpoller.example", False, "plain http"),
    ("ftp://box.ts.net", False, "must start with"),
    ("box.ts.net", False, "must start with"),
    ("", False, "must start with"),
])
def test_is_trusted_transport(url, ok, reason):
    got_ok, got_reason = config.is_trusted_transport(url)
    assert got_ok is ok, (url, got_reason)
    assert reason in got_reason


def _settings(monkeypatch, url):
    monkeypatch.setattr(config, "get_settings", lambda: {
        "auto_sync_enabled": True, "setup_mode": config.SETUP_MODE_PAIRED,
        "posting_server_url": url, "posting_server_api_key": "k" * 40,
    })


def test_auto_sync_target_accepts_tailscale_and_refuses_lan_http(monkeypatch):
    _settings(monkeypatch, "http://box.tail1234.ts.net:8420/")
    assert auto_sync._sync_target() == ("http://box.tail1234.ts.net:8420", "k" * 40)
    _settings(monkeypatch, "http://100.100.1.2:8420")
    assert auto_sync._sync_target() == ("http://100.100.1.2:8420", "k" * 40)
    _settings(monkeypatch, "http://192.168.1.20:8420")
    assert auto_sync._sync_target() is None
    _settings(monkeypatch, "https://pawpoller.example")
    assert auto_sync._sync_target() == ("https://pawpoller.example", "k" * 40)


def test_auto_sync_still_never_targets_loopback_or_itself(monkeypatch):
    _settings(monkeypatch, "http://127.0.0.1:8420")
    assert auto_sync._sync_target() is None
    monkeypatch.setattr(config, "get_settings", lambda: {
        "auto_sync_enabled": True, "setup_mode": config.SETUP_MODE_SERVER,
        "posting_server_url": "https://elsewhere.example", "posting_server_api_key": "k" * 40,
    })
    assert auto_sync._sync_target() is None


def test_tailscale_state_reports_the_cli_view():
    """4.14.0: the wizard's Connect test says whether the tunnel is actually up."""
    assert settings_api.tailscale_state("https://pawpoller.example") is None
    assert settings_api.tailscale_state("http://192.168.1.2:8420") is None
    absent = settings_api.tailscale_state("http://box.tail1.ts.net:8420", which=lambda name: None)
    assert absent == {"present": False, "state": "not installed", "self": ""}

    class P:
        stdout = '{"BackendState": "Running", "Self": {"DNSName": "laptop.tail1.ts.net."}}'

    got = settings_api.tailscale_state("http://100.100.1.2:8420", which=lambda n: "/usr/bin/tailscale" if n == "tailscale" else None,
                                       run=lambda cmd: P())
    assert got == {"present": True, "state": "Running", "self": "laptop.tail1.ts.net"}

    def boom(cmd):
        raise TimeoutError()

    got = settings_api.tailscale_state("http://box.ts.net", which=lambda n: "x", run=boom)
    assert got["present"] is True and got["state"].startswith("error: TimeoutError")


def test_tailscale_hint_only_speaks_when_the_tunnel_is_the_likely_problem():
    assert settings_api.tailscale_hint(None) == ""
    assert settings_api.tailscale_hint({"present": True, "state": "Running", "self": "a.ts.net"}) == ""
    assert "not installed" in settings_api.tailscale_hint({"present": False, "state": "not installed", "self": ""})
    assert "NeedsLogin" in settings_api.tailscale_hint({"present": True, "state": "NeedsLogin", "self": ""})


@pytest.mark.asyncio
async def test_pair_test_names_tailscale_when_a_ts_url_is_unreachable(monkeypatch):
    import httpx

    def _down(*a, **k):
        raise OSError("connection refused")

    monkeypatch.setattr(httpx, "AsyncClient", _down)
    monkeypatch.setattr(settings_api, "tailscale_state",
                        lambda url: {"present": False, "state": "not installed", "self": ""})
    Req = settings_api.PairingTestRequest
    out = await settings_api.pair_test(Req(posting_server_url="http://box.tail1.ts.net:8420", posting_server_api_key="k" * 40))
    assert out["ok"] is False
    assert "Could not reach server" in out["error"] and "Tailscale is not installed" in out["error"]

    monkeypatch.setattr(settings_api, "tailscale_state", lambda url: None)
    out = await settings_api.pair_test(Req(posting_server_url="https://pawpoller.example", posting_server_api_key="k" * 40))
    assert out["ok"] is False and "Tailscale" not in out["error"]


@pytest.mark.asyncio
async def test_pair_test_uses_the_same_rule(monkeypatch):
    import httpx

    class _Boom(Exception):
        pass

    def _no_network(*a, **k):
        raise _Boom("reached the network")

    monkeypatch.setattr(httpx, "AsyncClient", _no_network)
    Req = settings_api.PairingTestRequest
    out = await settings_api.pair_test(Req(posting_server_url="http://192.168.1.20:8420", posting_server_api_key="k" * 40))
    assert out["ok"] is False and "Tailscale" in out["error"]

    async def reached(url):
        # A trusted URL gets past the rule and on to the network — which the stub turns into
        # either an exception or an "ok: False" carrying its message, depending on the route's
        # own error handling. Either proves the point.
        try:
            res = await settings_api.pair_test(Req(posting_server_url=url, posting_server_api_key="k" * 40))
        except _Boom:
            return True
        return "reached the network" in str(res.get("error", ""))

    assert await reached("http://box.tail1234.ts.net:8420")
    assert await reached("https://pawpoller.example")
