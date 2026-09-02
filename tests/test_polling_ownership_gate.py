"""Manual poll triggers must respect polling ownership (mirroring spec, P1).

``config.get_polling_owner()`` gated the background poll LOOP and nothing else.
Every *manual* trigger — /api/poll/trigger, /poll/full-resync, and the pair on
each of the 19 platform routers — ran the identical cycle with no ownership
check, so a paired desktop clicking "Poll now" wrote the analytics tables the
server owns while the server was polling them too.

The guard lives in ``polling.background.spawn_poll`` rather than at the ~39 call
sites, so these tests exercise it there *and* through the real routers, to prove
the wiring actually happened in every platform module.

See docs/specs/desktop_server_mirroring.md §0.1 / P1.
"""
import asyncio

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import config
from polling import background


def _paired(monkeypatch):
    """This process is a desktop paired to a server → the server owns polling."""
    monkeypatch.setattr(background, "spawn", _boom)
    monkeypatch.setattr(config, "get_polling_owner", lambda runtime: "server")


def _record(calls):
    """Stand-in for spawn(). Closes the coroutine so the test run stays warning-free."""
    def _fake(coro, label):
        coro.close()
        calls.append(label)
    return _fake


def _owner(monkeypatch, calls):
    monkeypatch.setattr(config, "get_polling_owner", lambda runtime: "local")
    monkeypatch.setattr(background, "spawn", _record(calls))


def _boom(coro, label):
    raise AssertionError(f"a non-owner must never start the poll {label!r}")


async def _noop():
    return None


# ── The guard itself ───────────────────────────────────────────

def test_non_owner_is_refused_with_409(monkeypatch):
    _paired(monkeypatch)
    coro = _noop()
    with pytest.raises(HTTPException) as exc:
        background.spawn_poll(coro, "run_fa_poll_cycle")
    assert exc.value.status_code == 409
    assert "owns polling" in exc.value.detail
    coro.close()


def test_owner_runs_the_poll(monkeypatch):
    calls = []
    _owner(monkeypatch, calls)
    background.spawn_poll(_noop(), "run_fa_poll_cycle")
    assert calls == ["run_fa_poll_cycle"]


def test_ownership_is_resolved_per_call_not_cached(monkeypatch):
    """Pairing is toggled in Settings without a restart, so a cached answer lies."""
    calls = []
    owner = {"who": "server"}
    monkeypatch.setattr(config, "get_polling_owner", lambda runtime: owner["who"])
    monkeypatch.setattr(background, "spawn", _record(calls))

    coro = _noop()
    with pytest.raises(HTTPException):
        background.spawn_poll(coro, "first")
    coro.close()

    owner["who"] = "local"                      # user unpairs
    background.spawn_poll(_noop(), "second")
    assert calls == ["second"]


@pytest.mark.asyncio
async def test_plain_spawn_is_not_gated(monkeypatch):
    """The session-health check is read-only and stays available when paired."""
    monkeypatch.setattr(config, "get_polling_owner", lambda runtime: "server")
    seen = []

    async def work():
        seen.append("ran")

    background.spawn(work(), "manual-session-check")
    for _ in range(20):                      # let the detached task run
        await asyncio.sleep(0)
        if seen:
            break
    assert seen == ["ran"]


def test_refusal_closes_the_coroutine(monkeypatch):
    """Otherwise every refused click logs 'coroutine was never awaited'."""
    _paired(monkeypatch)
    coro = _noop()
    with pytest.raises(HTTPException):
        background.spawn_poll(coro, "run_fa_poll_cycle")
    with pytest.raises(RuntimeError, match="cannot reuse"):
        coro.send(None)                      # already closed


# ── Every platform router is actually wired to it ──────────────
# A per-platform trigger that still calls bare spawn() would pass every test
# above while leaving the hole wide open, so assert against the real routes.

PLATFORM_PREFIXES = [
    "ao3", "bsky", "da", "e621", "fa", "fbr", "fn", "ig", "ik",
    "mast", "pix", "sf", "sqw", "thr", "tum", "tw", "wp", "ws",
]


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(background, "spawn", _boom)
    monkeypatch.setattr(config, "get_polling_owner", lambda runtime: "server")
    monkeypatch.setattr(config, "is_dashboard_auth_required", lambda: False)
    import dashboard
    return TestClient(dashboard.app, raise_server_exceptions=False)


@pytest.mark.parametrize("prefix", PLATFORM_PREFIXES)
def test_every_platform_trigger_is_gated(client, prefix):
    r = client.post(f"/api/{prefix}/poll/trigger")
    assert r.status_code == 409, f"{prefix} poll trigger is not ownership-gated"


@pytest.mark.parametrize("prefix", PLATFORM_PREFIXES)
def test_every_platform_full_resync_is_gated(client, prefix):
    r = client.post(f"/api/{prefix}/poll/full-resync")
    assert r.status_code == 409, f"{prefix} full-resync is not ownership-gated"


@pytest.mark.parametrize("path", [
    "/api/poll/trigger",
    "/api/poll/full-resync",
    "/api/poll/trigger/fa",
])
def test_global_poll_routes_are_gated(client, path):
    assert client.post(path).status_code == 409
