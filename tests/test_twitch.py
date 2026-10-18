"""Twitch (ttv) — spec 045: the Helix client, the poll, the channel + webhook routes. No network.

Channels and ids are made up; Helix shapes follow Twitch's reference responses.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

import config
from clients.ttv import client as ttv
from clients.ttv.client import TtvAuthError, TtvClient, clean_login, duration_seconds, parse_stream
from database.db import get_connection

USER = {"id": "1001", "login": "inkwolf", "display_name": "Inkwolf", "profile_image_url": ""}
STREAM = {"id": "s-77", "user_id": "1001", "user_login": "inkwolf", "user_name": "Inkwolf", "game_name": "Art",
          "type": "live", "title": "Sketching", "viewer_count": 12, "started_at": "2026-10-19T01:00:00Z",
          "is_mature": False}
VIDEO = {"id": "v1", "stream_id": "s-70", "title": "Last week", "url": "https://www.twitch.tv/videos/1",
         "thumbnail_url": "https://x/%{width}x%{height}.jpg", "view_count": 40, "created_at": "2026-10-12T01:00:00Z",
         "duration": "1h2m3s"}
CLIP = {"id": "Funny", "url": "https://clips.twitch.tv/Funny", "title": "A moment", "view_count": 9,
        "created_at": "2026-10-13T01:00:00Z", "thumbnail_url": "t.jpg", "duration": 21.5}


@pytest.fixture(autouse=True)
def _fresh_tokens():
    ttv._APP_TOKENS.clear()
    yield
    ttv._APP_TOKENS.clear()


def make(handler, **kw):
    c = TtvClient("cid", "sec", **kw)
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return c


def helix(routes, calls=None, *, token_status=200, follow=None):
    """A Twitch double: id.twitch.tv tokens + Helix paths from `routes` ({path: data list or dict})."""
    def handler(req: httpx.Request):
        if calls is not None:
            calls.append((req.method, req.url.path, dict(req.url.params), req.headers.get("authorization", "")))
        if req.url.host == "id.twitch.tv":
            if req.url.path.endswith("/validate"):
                return httpx.Response(200, json={"login": "inkwolf", "user_id": "1001", "scopes": []})
            return httpx.Response(token_status, json={"access_token": "app-tok", "expires_in": 3600,
                                                      "refresh_token": "r2"})
        body = routes.get(req.url.path.replace("/helix", ""), [])
        return httpx.Response(200, json=body if isinstance(body, dict) else {"data": body})
    return handler


# ── client ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,want", [
    ("inkwolf", "inkwolf"), (" @InkWolf ", "inkwolf"), ("https://www.twitch.tv/inkwolf/videos", "inkwolf"),
    ("has space", ""), ("../etc", ""), ("", ""),
])
def test_channel_names_are_cleaned(raw, want):
    assert clean_login(raw) == want


@pytest.mark.parametrize("raw,want", [("1h2m3s", 3723), ("45m", 2700), ("9s", 9), (21.5, 21), ("", 0), ("x", 0)])
def test_durations(raw, want):
    assert duration_seconds(raw) == want


def test_a_stream_keeps_what_go_live_needs():
    s = parse_stream(STREAM)
    assert s == {"stream_key": "s-77", "user_id": "1001", "login": "inkwolf", "name": "Inkwolf", "title": "Sketching",
                 "game": "Art", "viewers": 12, "started_at": "2026-10-19 01:00:00", "mature": False,
                 "link": "https://www.twitch.tv/inkwolf"}


def test_the_app_token_is_asked_for_once_and_renewed_when_refused():
    calls, state = [], {"n": 0}

    def handler(req):
        calls.append(req.url.host)
        if req.url.host == "id.twitch.tv":
            return httpx.Response(200, json={"access_token": f"t{len(calls)}", "expires_in": 3600})
        state["n"] += 1
        if state["n"] == 2:
            return httpx.Response(401)
        return httpx.Response(200, json={"data": [USER]})
    c = make(handler, login="inkwolf")
    assert asyncio.run(c.get_user())["user_id"] == "1001"
    assert asyncio.run(c.get_user())["user_id"] == "1001"          # 401 → a fresh token → retried
    assert calls.count("id.twitch.tv") == 2


def test_bad_app_credentials_say_so():
    c = make(helix({}, token_status=400), login="inkwolf")
    with pytest.raises(TtvAuthError, match="client id or secret"):
        asyncio.run(c.get_user())


def test_live_check_batches_every_channel_in_one_request():
    calls = []
    c = make(helix({"/streams": [STREAM]}, calls))
    live = asyncio.run(c.get_streams(["1001", "1002", "1001"]))
    assert list(live) == ["1001"]
    streams = [x for x in calls if x[1] == "/helix/streams"]
    assert len(streams) == 1


def test_the_private_numbers_need_the_streamers_connection():
    c = make(helix({}), user_id="1001")
    assert asyncio.run(c.get_follower_count()) is None             # no Connect → no figure, not 0


# ── poll ─────────────────────────────────────────────────────────────────────

def _account():
    from database import accounts as adb
    conn = get_connection()
    try:
        return adb.get_default_account_id(conn, "ttv", create=True)
    finally:
        conn.close()


def test_a_poll_stores_live_state_broadcasts_and_clips():
    from polling.ttv_poller import run_ttv_poll_cycle
    config.save_settings({"ttv_channel": "inkwolf", "ttv_user_id": "1001"})
    aid = _account()
    routes = {"/users": [USER], "/streams": [STREAM], "/videos": [VIDEO], "/clips": [CLIP]}
    for _ in range(2):                                             # twice: items upsert, snapshots add
        asyncio.run(run_ttv_poll_cycle(aid, client=make(helix(routes), login="inkwolf", user_id="1001")))
    conn = get_connection()
    try:
        items = {r["submission_id"]: dict(r) for r in conn.execute("SELECT * FROM ttv_submissions")}
        assert set(items) == {"vv1", "cFunny"}
        assert items["vv1"]["duration_s"] == 3723 and items["vv1"]["thumbnail_url"] == "https://x/640x360.jpg"
        assert conn.execute("SELECT COUNT(*) FROM ttv_snapshots").fetchone()[0] == 4
        ch = dict(conn.execute("SELECT * FROM ttv_channel_snapshots ORDER BY id DESC").fetchone())
        assert ch["live"] == 1 and ch["viewers"] == 12 and ch["title"] == "Sketching"
        assert ch["followers"] is None and ch["connected"] == 0       # Twitch keeps those private
        assert conn.execute("SELECT live_state FROM golive_channels").fetchone()[0] == "live"
    finally:
        conn.close()


def test_a_connected_poll_reads_followers_subscribers_and_bits():
    from polling.ttv_poller import run_ttv_poll_cycle
    config.save_settings({"ttv_channel": "inkwolf", "ttv_user_id": "1001"})
    aid = _account()
    routes = {"/users": [USER], "/streams": [], "/videos": [], "/clips": [],
              "/channels/followers": {"total": 321, "data": []}, "/subscriptions": {"total": 7, "points": 9, "data": []},
              "/bits/leaderboard": {"data": [{"score": 100}, {"score": 50}]}}
    client = make(helix(routes), login="inkwolf", user_id="1001", access_token="u", refresh_token="r",
                  token_expires_at=9e12)
    asyncio.run(run_ttv_poll_cycle(aid, client=client))
    conn = get_connection()
    try:
        ch = dict(conn.execute("SELECT * FROM ttv_channel_snapshots").fetchone())
        assert (ch["followers"], ch["subscribers"], ch["sub_points"], ch["bits_top"]) == (321, 7, 9, 150)
        assert conn.execute("SELECT followers FROM account_follower_snapshots WHERE account_id = ?",
                            (aid,)).fetchone()[0] == 321
    finally:
        conn.close()


def test_a_renamed_channel_keeps_its_id_and_takes_the_new_name():
    from polling.ttv_poller import run_ttv_poll_cycle
    config.save_settings({"ttv_channel": "oldname", "ttv_user_id": "1001"})
    routes = {"/users": [USER], "/streams": [], "/videos": [], "/clips": []}
    asyncio.run(run_ttv_poll_cycle(_account(), client=make(helix(routes), login="oldname", user_id="1001")))
    assert config.get_settings()["ttv_channel"] == "inkwolf"


# ── routes ───────────────────────────────────────────────────────────────────

@pytest.fixture
def api():
    import dashboard
    return TestClient(dashboard.app, client=("127.0.0.1", 50000))


def test_saving_the_channel_stores_twitchs_id(api, monkeypatch):
    import routes.ttv_api as r
    monkeypatch.setattr(r, "make_client", lambda creds: make(helix({"/users": [USER]}), login=creds.get("ttv_channel", "")))
    res = api.post("/api/ttv/channel", json={"channel": "twitch.tv/InkWolf", "client_id": "cid", "client_secret": "sec"})
    assert res.status_code == 200, res.text
    s = config.get_settings()
    assert s["ttv_user_id"] == "1001" and s["ttv_channel"] == "inkwolf" and s["ttv_client_id"] == "cid"
    status = api.get("/api/ttv/channel/status").json()
    assert status["has_credentials"] and status["listening"] == "check" and "ttv_client_secret" not in status


def test_saving_needs_the_app_key(api, monkeypatch):
    monkeypatch.delenv("PAWPOLLER_TWITCH_CLIENT_ID", raising=False)
    res = api.post("/api/ttv/channel", json={"channel": "inkwolf"})
    assert res.status_code == 400 and "client id" in res.json()["detail"]


def test_an_unknown_channel_is_refused(api, monkeypatch):
    import routes.ttv_api as r
    monkeypatch.setattr(r, "make_client", lambda creds: make(helix({"/users": []})))
    res = api.post("/api/ttv/channel", json={"channel": "nosuch", "client_id": "cid", "client_secret": "sec"})
    assert res.status_code == 400 and "No Twitch channel" in res.json()["detail"]


def test_a_decided_go_live_cannot_be_posted_again(api, monkeypatch):
    from polling import golive
    aid = _account()
    golive.save_config("ttv", aid, {"enabled": True})
    rid = asyncio.run(golive.on_online("ttv", aid, {"stream_key": "k", "title": "T", "started_at": golive._now()},
                                       detected_by="signal"))
    assert api.post(f"/api/ttv/golive/runs/{rid}/skip").json() == {"state": "skipped"}
    res = api.post(f"/api/ttv/golive/runs/{rid}/post")
    assert res.status_code == 409


def test_go_live_settings_round_trip_and_refuse_a_bad_cooldown(api):
    aid = _account()
    assert api.put("/api/ttv/golive", json={"account_id": aid, "cooldown_min": 99}).status_code == 422
    assert api.put("/api/ttv/golive", json={"account_id": aid, "enabled": True, "cooldown_min": 5}).status_code == 200
    got = api.get("/api/ttv/golive", params={"account_id": aid}).json()
    assert got["channel"]["enabled"] == 1 and got["channel"]["cooldown_min"] == 5 and "title" in got["placeholders"]


# ── the webhook (SC-004) ─────────────────────────────────────────────────────

SECRET = "s3cret-for-tests"


def signed(api, body: dict, kind="notification", *, mid="m1", ts=None, secret=SECRET):
    raw = json.dumps(body).encode()
    ts = ts or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.123456789Z")
    sig = "sha256=" + hmac.new(secret.encode(), mid.encode() + ts.encode() + raw, hashlib.sha256).hexdigest()
    return api.post("/hooks/twitch", content=raw, headers={
        "Twitch-Eventsub-Message-Id": mid, "Twitch-Eventsub-Message-Timestamp": ts,
        "Twitch-Eventsub-Message-Signature": sig, "Twitch-Eventsub-Message-Type": kind,
        "Content-Type": "application/json"})


@pytest.fixture
def hook(api, monkeypatch):
    monkeypatch.setenv("PAWPOLLER_PUBLIC_BASE_URL", "https://paw-inkwolf.pawpoller.com")
    config.save_settings({"ttv_eventsub_secret": SECRET, "ttv_user_id": "1001", "ttv_channel": "inkwolf"})
    from polling import golive
    aid = _account()
    golive.save_config("ttv", aid, {"enabled": True})
    seen = []

    async def fake_online(a, ev):
        seen.append((a, ev.get("id")))
    import routes.ttv_api as r
    monkeypatch.setattr(r, "_online_from_signal", fake_online)
    return {"aid": aid, "seen": seen}


ONLINE = {"subscription": {"type": "stream.online", "condition": {"broadcaster_user_id": "1001"}},
          "event": {"id": "s-77", "broadcaster_user_id": "1001", "broadcaster_user_login": "inkwolf",
                    "started_at": "2026-10-19T01:00:00Z"}}


def test_the_webhook_answers_twitchs_challenge(api, hook):
    res = signed(api, {"challenge": "pogchamp-kappa", "subscription": {}}, kind="webhook_callback_verification")
    assert res.status_code == 200 and res.text == "pogchamp-kappa"


def test_a_forged_delivery_is_refused(api, hook):
    assert signed(api, ONLINE, secret="wrong").status_code == 403
    assert hook["seen"] == []


def test_a_stale_delivery_is_refused(api, hook):
    old = (datetime.now(timezone.utc) - timedelta(minutes=11)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert signed(api, ONLINE, ts=old).status_code == 403


def test_a_replayed_delivery_is_ignored(api, hook):
    assert signed(api, ONLINE, mid="same").status_code == 204
    assert signed(api, ONLINE, mid="same").status_code == 204
    assert hook["seen"] == [(hook["aid"], "s-77")]


def test_stream_offline_ends_the_stream(api, hook):
    from polling import golive
    asyncio.run(golive.on_online("ttv", hook["aid"], {"stream_key": "s-77", "started_at": golive._now()},
                                 detected_by="signal"))
    off = {"subscription": {"type": "stream.offline", "condition": {"broadcaster_user_id": "1001"}}, "event": {}}
    assert signed(api, off, mid="m2").status_code == 204
    conn = get_connection()
    try:
        assert conn.execute("SELECT state FROM golive_streams").fetchone()[0] == "ended"
    finally:
        conn.close()


def test_without_a_public_address_the_webhook_is_not_there(api, monkeypatch):
    monkeypatch.delenv("PAWPOLLER_PUBLIC_BASE_URL", raising=False)
    assert api.post("/hooks/twitch", content=b"{}").status_code == 404


def test_the_webhook_needs_no_sign_in_but_the_api_does():
    import dashboard
    assert "/hooks/twitch" in dashboard._AUTH_EXEMPT_PATHS
    assert not any("/api/ttv".startswith(p) for p in dashboard._AUTH_EXEMPT_PREFIXES)
    assert dashboard._PUBLISH_WHEN_OPEN.match("/api/ttv/golive/runs/3/post")
