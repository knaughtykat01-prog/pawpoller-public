"""Server-update endpoints: auth/loopback gates and the request→claim lifecycle (4.32.0, spec 002).

Calls the route coroutines directly with a stub Request so we can exercise the loopback gate
and the single-shot claim without a live server. `_is_server` and the release check are
monkeypatched so the tests don't depend on runtime mode or the network.
"""
import asyncio

import pytest
from fastapi import HTTPException

import routes.server_update_api as m


class _Client:
    def __init__(self, host):
        self.host = host


class _Req:
    def __init__(self, host):
        self.client = _Client(host)


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def flags(tmp_path, monkeypatch):
    req = tmp_path / ".update-request"
    seen = tmp_path / ".update-agent-seen"
    monkeypatch.setattr(m, "_REQUEST_FILE", req)
    monkeypatch.setattr(m, "_AGENT_SEEN_FILE", seen)
    return req, seen


def test_claim_rejects_non_loopback(flags):
    with pytest.raises(HTTPException) as e:
        _run(m.claim_update(_Req("testclient")))
    assert e.value.status_code == 403


def test_claim_loopback_records_heartbeat_with_no_request(flags):
    _req, seen = flags
    out = _run(m.claim_update(_Req("127.0.0.1")))
    assert out == {"requested": False}
    assert seen.exists()  # heartbeat written even when nothing is pending


def test_request_then_claimed_exactly_once(flags, monkeypatch):
    req, _seen = flags
    monkeypatch.setattr(m, "_is_server", lambda: True)
    r = _run(m.request_update())
    assert r["status"] == "requested" and req.exists()
    first = _run(m.claim_update(_Req("127.0.0.1")))
    assert first == {"requested": True}
    assert not req.exists()  # cleared on claim
    second = _run(m.claim_update(_Req("::1")))
    assert second == {"requested": False}  # a second claim gets nothing (FR-015)


def test_request_refused_on_a_desktop_instance(flags, monkeypatch):
    monkeypatch.setattr(m, "_is_server", lambda: False)
    with pytest.raises(HTTPException) as e:
        _run(m.request_update())
    assert e.value.status_code == 400


def test_stale_request_is_not_honoured(flags, monkeypatch):
    req, _seen = flags
    monkeypatch.setattr(m, "_REQUEST_TTL", 0)  # everything is instantly stale
    monkeypatch.setattr(m, "_is_server", lambda: True)
    _run(m.request_update())
    out = _run(m.claim_update(_Req("127.0.0.1")))
    assert out == {"requested": False}  # too old → not fired
    assert not req.exists()             # but still cleared


def test_status_shape_and_availability(flags, monkeypatch):
    monkeypatch.setattr(m, "_is_server", lambda: True)
    import updater
    monkeypatch.setattr(updater, "check_for_update",
                        lambda: {"available": True, "current": "1.0.0", "latest": "2.0.0"})
    out = _run(m.update_status())
    assert out["applicable"] is True
    assert out["available"] is True
    assert out["latest"] == "2.0.0"
    assert set(out) >= {"applicable", "current", "latest", "available", "host_agent_installed", "in_progress"}


def test_status_non_applicable_on_desktop_skips_network(flags, monkeypatch):
    monkeypatch.setattr(m, "_is_server", lambda: False)
    import updater

    def _boom():
        raise AssertionError("release check must not run on a desktop instance")

    monkeypatch.setattr(updater, "check_for_update", _boom)
    out = _run(m.update_status())
    assert out["applicable"] is False
    assert out["available"] is False
    assert out["latest"] is None
