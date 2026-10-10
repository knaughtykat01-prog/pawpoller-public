"""A GIF Mastodon is still processing is waited for, not posted early (MASTGIF422).

Found during the brand launch (2026-10-07): `_upload_media` took a 202 (still
processing) media id as safe to attach straight away, on the belief that
Mastodon holds the status until the media is ready. mastodon.social instead
answered 422 on /api/v1/statuses for a GIF it was still converting, and
PawPoller reported that as "the access token needs a write scope", which sent
the diagnosis the wrong way. Now a 202 is polled at GET /api/v1/media/:id until
its url is set, and a refused post says what Mastodon said.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest
import respx

from clients.mast import client as mast

BASE = "https://mastodon.example"
ACCOUNT = {"id": "1", "acct": "pawpoller", "username": "pawpoller"}
STATUS = {"id": "99", "uri": f"{BASE}/users/pawpoller/statuses/99",
          "url": f"{BASE}/@pawpoller/99"}


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    async def _instant(_s):
        return None
    monkeypatch.setattr(mast.asyncio, "sleep", _instant)


@pytest.fixture
def gif(tmp_path):
    p = tmp_path / "launch.gif"
    p.write_bytes(b"GIF89a" + b"\0" * 32)
    return str(p)


def _post(gif):
    async def go():
        c = mast.MastClient(instance_url=BASE, access_token="tok")
        try:
            return await c.create_status("Hello", image_paths=[gif]), c.last_error
        finally:
            await c.close()
    return asyncio.run(go())


@respx.mock
def test_a_processing_gif_is_waited_for_before_the_status(gif):
    respx.get(f"{BASE}/api/v1/accounts/verify_credentials").respond(200, json=ACCOUNT)
    respx.post(f"{BASE}/api/v2/media").respond(202, json={"id": "m1", "url": None})
    checks = respx.get(f"{BASE}/api/v1/media/m1").mock(side_effect=[
        httpx.Response(206, json={"id": "m1", "url": None}),
        httpx.Response(206, json={"id": "m1", "url": None}),
        httpx.Response(200, json={"id": "m1", "url": f"{BASE}/media/m1.mp4"}),
    ])

    def _status(request):
        # Posting before the media was ready is exactly what mastodon.social refuses.
        assert checks.call_count == 3, "posted before the media finished processing"
        assert b"media_ids" in request.content
        return httpx.Response(200, json=STATUS)
    respx.post(f"{BASE}/api/v1/statuses").mock(side_effect=_status)

    r, err = _post(gif)
    assert r and r["id"] == "99" and err == ""


@respx.mock
def test_a_ready_image_is_not_polled(gif):
    respx.get(f"{BASE}/api/v1/accounts/verify_credentials").respond(200, json=ACCOUNT)
    respx.post(f"{BASE}/api/v2/media").respond(200, json={"id": "m2", "url": f"{BASE}/m2.png"})
    checks = respx.get(f"{BASE}/api/v1/media/m2").respond(200, json={})
    respx.post(f"{BASE}/api/v1/statuses").respond(200, json=STATUS)
    r, _ = _post(gif)
    assert r and checks.call_count == 0


@respx.mock
def test_a_422_says_what_mastodon_said_not_write_scope(gif):
    respx.get(f"{BASE}/api/v1/accounts/verify_credentials").respond(200, json=ACCOUNT)
    respx.post(f"{BASE}/api/v2/media").respond(200, json={"id": "m3", "url": f"{BASE}/m3.png"})
    respx.post(f"{BASE}/api/v1/statuses").respond(
        422, json={"error": "Validation failed: Text character limit of 500 exceeded"})
    r, err = _post(gif)
    assert r is None
    assert "422" in err and "character limit" in err
    assert "scope" not in err


@respx.mock
def test_media_that_never_finishes_gives_up_with_a_clear_reason(gif, monkeypatch):
    monkeypatch.setattr(mast.MastClient, "_MEDIA_WAIT_S", 5.0)
    respx.get(f"{BASE}/api/v1/accounts/verify_credentials").respond(200, json=ACCOUNT)
    respx.post(f"{BASE}/api/v2/media").respond(202, json={"id": "m4", "url": None})
    respx.get(f"{BASE}/api/v1/media/m4").respond(206, json={"id": "m4", "url": None})
    statuses = respx.post(f"{BASE}/api/v1/statuses").respond(200, json=STATUS)
    r, err = _post(gif)
    assert r is None and statuses.call_count == 0
    assert "still processing" in err


@respx.mock
def test_a_403_still_names_the_missing_scope(gif):
    respx.get(f"{BASE}/api/v1/accounts/verify_credentials").respond(200, json=ACCOUNT)
    respx.post(f"{BASE}/api/v1/statuses").respond(403, json={"error": "This action is outside the authorized scopes"})

    async def go():
        c = mast.MastClient(instance_url=BASE, access_token="tok")
        try:
            return await c.create_status("Hello"), c.last_error
        finally:
            await c.close()
    r, err = asyncio.run(go())
    assert r is None and "write:statuses" in err
