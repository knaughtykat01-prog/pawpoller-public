"""Picarto (pic) — spec 013 (4.46.0). Channel analytics from the public API, no login.

Fixtures use Picarto's own official channel name or made-up ones, never a real artist.
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

import config
from clients.pic.client import PicClient, clean_channel, parse_video
from database import pic_queries
from database.db import get_connection

CHANNEL = {"user_id": 44, "name": "Picarto", "online": False, "viewers": 0, "viewers_total": 4566,
           "followers": 115, "subscribers": 2, "adult": False, "recordings": True,
           "last_live": "2022-01-26 15:00:59", "title": "Picarto.tv", "avatar": ""}
VIDEO = {"id": 819661, "title": "Sketch stream", "duration": 35596915, "views": 0, "adult": True,
         "timestamp": "2023-11-23T08:33:21.000000Z", "thumbnails": {"web": "w.jpg", "web_large": "wl.jpg"}}


def _client(channel="picarto", *, channel_body=CHANNEL, videos=(VIDEO,), status=200, calls=None):
    def handler(req: httpx.Request):
        if calls is not None:
            calls.append(req.url.path)
        if status != 200:
            return httpx.Response(status, json="Channel does not exist")
        if req.url.path.endswith("/videos"):
            return httpx.Response(200, json=list(videos))
        return httpx.Response(200, json=channel_body)
    c = PicClient(channel)
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return c


# ── client ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,want", [
    ("Picarto", "Picarto"), ("  @Picarto ", "Picarto"), ("https://picarto.tv/Picarto", "Picarto"),
    ("picarto.tv/Some_Name/profile", "Some_Name"), ("has space", ""), ("../etc", ""), ("", ""),
])
def test_channel_names_are_cleaned(raw, want):
    assert clean_channel(raw) == want


def test_a_recording_keeps_milliseconds_and_the_adult_flag():
    r = parse_video(VIDEO, "Picarto")
    assert r["duration_ms"] == 35596915                 # ~9.9 h: Picarto sends ms despite its docs
    assert r["adult"] == 1 and r["posted_at"] == "2023-11-23 08:33:21"
    assert r["link"] == "https://picarto.tv/Picarto/profile/videos/819661" and r["thumbnail_url"] == "wl.jpg"


@pytest.mark.parametrize("status", [404, 500])
def test_a_missing_channel_is_not_found_not_a_crash(status):
    c = _client("NoSuchChannelXyz", status=status)
    assert asyncio.run(c.validate_session()) is None


def test_an_adult_channel_is_read_by_name():
    calls = []
    c = _client("Picarto", channel_body={**CHANNEL, "adult": True}, calls=calls)
    ch = asyncio.run(c.get_channel())
    assert ch["adult"] is True and ch["views"] == 4566
    assert all("/online" not in p and "/search" not in p for p in calls)   # lists that hide adult channels


def test_rate_limit_is_a_plain_error():
    from clients.pic.client import PicError
    c = _client(status=429)
    with pytest.raises(PicError, match="rate-limiting"):
        asyncio.run(c.get_channel())


# ── poll ─────────────────────────────────────────────────────────────────────

def _default_account():
    from database import accounts as adb
    conn = get_connection()
    try:
        return adb.get_default_account_id(conn, "pic", create=True)
    finally:
        conn.close()


def test_a_poll_stores_the_channel_followers_and_recordings_once():
    from polling.pic_poller import run_pic_poll_cycle
    config.save_settings({"pic_channel": "Picarto"})
    aid = _default_account()
    for _ in range(2):                                         # twice: recordings upsert, never duplicate
        asyncio.run(run_pic_poll_cycle(aid, client=_client()))
    conn = get_connection()
    try:
        assert conn.execute("SELECT COUNT(*) FROM pic_submissions").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM pic_channel_snapshots").fetchone()[0] == 2
        assert conn.execute("SELECT followers FROM account_follower_snapshots WHERE account_id = ?",
                            (aid,)).fetchone()[0] == 115
        s = pic_queries.get_pic_summary(conn)
        assert s["total_views"] == 4566 and s["followers"] == 115 and s["subscribers"] == 2
        assert s["total_submissions"] == 1 and s["channels"][0]["name"] == "Picarto"
        assert conn.execute("SELECT status FROM pic_poll_log ORDER BY id DESC").fetchone()[0] == "success"
    finally:
        conn.close()


def test_no_recordings_is_a_normal_poll():
    from polling.pic_poller import run_pic_poll_cycle
    config.save_settings({"pic_channel": "Picarto"})
    stats = asyncio.run(run_pic_poll_cycle(_default_account(),
                                           client=_client(channel_body={**CHANNEL, "recordings": False}, videos=())))
    assert stats == {"submissions_found": 0, "snapshots_inserted": 1}


def test_a_recording_gone_from_picarto_stays_in_pawpoller():
    from polling.pic_poller import run_pic_poll_cycle
    config.save_settings({"pic_channel": "Picarto"})
    aid = _default_account()
    asyncio.run(run_pic_poll_cycle(aid, client=_client()))
    asyncio.run(run_pic_poll_cycle(aid, client=_client(videos=())))
    conn = get_connection()
    try:
        assert conn.execute("SELECT COUNT(*) FROM pic_submissions").fetchone()[0] == 1
    finally:
        conn.close()


def test_a_missing_channel_fails_the_poll_cleanly(monkeypatch):
    from polling import pic_poller

    async def quiet(*a, **k):
        return None
    monkeypatch.setattr("polling.telegram.send_poll_error", quiet)
    config.save_settings({"pic_channel": "NoSuchChannelXyz"})
    with pytest.raises(ValueError, match="no channel called"):
        asyncio.run(pic_poller.run_pic_poll_cycle(_default_account(), client=_client("NoSuchChannelXyz", status=404)))
    conn = get_connection()
    try:
        assert conn.execute("SELECT status FROM pic_poll_log ORDER BY id DESC").fetchone()[0] == "error"
    finally:
        conn.close()


# ── API ──────────────────────────────────────────────────────────────────────

def _api():
    import dashboard
    return TestClient(dashboard.app, raise_server_exceptions=False, client=("127.0.0.1", 50000))


def test_saving_a_channel_checks_it_and_keeps_picartos_spelling(monkeypatch):
    import clients.pic.client as mod
    real = mod.PicClient

    def fake(name="", base_url=mod.API_BASE):
        return _client(name, status=200 if name.lower() == "picarto" else 404)
    monkeypatch.setattr(mod, "PicClient", fake)
    api = _api()
    r = api.post("/api/pic/channel", json={"channel": "https://picarto.tv/picarto"})
    assert r.status_code == 200 and r.json()["channel"] == "Picarto"
    assert config.get_settings()["pic_channel"] == "Picarto"
    assert api.post("/api/pic/channel", json={"channel": "NoSuchChannelXyz"}).status_code == 400
    assert api.post("/api/pic/channel", json={"channel": "not a name!"}).status_code == 400
    monkeypatch.setattr(mod, "PicClient", real)


def test_summary_and_aggregate_report_channel_numbers():
    conn = get_connection()
    try:
        aid = _default_account()
        for ts, views, fol in (("2026-10-01 00:00:00", 100, 10), ("2026-10-02 00:00:00", 150, 12)):
            pic_queries.insert_pic_channel_snapshot(
                conn, aid, {"name": "Picarto", "views": views, "followers": fol, "subscribers": 1}, ts)
        conn.commit()
    finally:
        conn.close()
    api = _api()
    s = api.get("/api/pic/summary").json()
    assert s["total_views"] == 150 and s["followers"] == 12 and "growth_rates" in s
    agg = api.get("/api/pic/aggregate").json()["snapshots"]
    assert [a["views"] for a in agg] == [100, 150] and agg[-1]["followers"] == 12


def test_the_account_test_button_names_the_channel(monkeypatch):
    from polling import account_probe
    monkeypatch.setitem(account_probe.PROBES, "pic",
                        (account_probe.PROBES["pic"][0], lambda c, pk: _client(c["pic_channel"]), "pic", False))
    ok = asyncio.run(account_probe._run("pic", _client("Picarto"), {"pic_channel": "Picarto"}))
    assert ok["status"] == "ok" and "Picarto" in ok["detail"]
    bad = asyncio.run(account_probe._run("pic", _client("Nope", status=404), {"pic_channel": "Nope"}))
    assert bad["status"] != "ok" and "no channel called Nope" in bad["detail"]


def test_no_login_data_is_asked_for():
    """FR-008: the channel name is a handle, not a secret, and it's the only field."""
    assert config.PLATFORM_CREDENTIAL_FIELDS["pic"] == ["pic_channel"]
    assert not config.is_credential_key("pic_channel")
    json.dumps(config.PLATFORM_CREDENTIAL_FIELDS["pic"])
