"""SoundCloud (`sc`) — MEDIAPLATS §3 (4.22.0), built against the documented shapes with a fake client.

Covers the client's pure parts (PKCE, the authorize URL, tag_list, parse_track, the token
rotation flag), the poll cycle end to end against a fake client (rows, snapshots, the
follower series, the rotated pair written back to the account's own keys), the OAuth routes
(connect → authorize URL + single-use state, the callback's exchange / mismatch refusal /
storage, disconnect), the poster (post / edit / refusals / validate) and every registry the
platform hangs off. No network anywhere.
"""
from __future__ import annotations

import asyncio
import inspect
import io
import re
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import config
from clients.sc import client as scc
from clients.sc.client import ScClient, authorize_url, pkce_pair, tag_list
from database.db import get_connection


def _png(w=64, h=36) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (200, 90, 120)).save(buf, format="PNG")
    return buf.getvalue()


FAKE_MP3 = b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\xff\xfb" * 100

TRACK = {
    "id": 123456, "urn": "soundcloud:tracks:123456", "title": "Sample Track", "description": "A tune.",
    "genre": "Ambient", "tag_list": '"field recording" night', "sharing": "public", "artwork_url": "https://i1.sndcdn.com/x.jpg",
    "duration": 190400, "created_at": "2026/09/01 10:00:00 +0000", "permalink_url": "https://soundcloud.com/samplehandle/sample-track",
    "playback_count": 42, "likes_count": 7, "comment_count": 2, "reposts_count": 1, "download_count": 0,
    "user": {"permalink": "samplehandle", "username": "Sample Handle", "followers_count": 12},
}


class FakeScClient:
    """The documented shapes, no HTTP."""

    def __init__(self, client_id="", client_secret="", access_token="", refresh_token="", expires_at=0.0):
        self.client_id, self.client_secret = client_id, client_secret
        self.access_token, self.refresh_token, self.expires_at = access_token, refresh_token, expires_at
        self.tokens_changed = False
        self._me = None
        self.uploads: list[dict] = []
        self.updates: list[tuple] = []
        self.tracks = [dict(TRACK)]
        self.me = {"permalink": "samplehandle", "username": "Sample Handle", "followers_count": 12}

    async def close(self):
        pass

    async def refresh(self):
        self.access_token, self.refresh_token = "at-new", "rt-new"
        self.expires_at = 4102444800.0
        self.tokens_changed = True
        return {}

    async def exchange_code(self, code, redirect_uri, verifier):
        assert code == "good-code" and verifier
        self.access_token, self.refresh_token, self.expires_at = "at-1", "rt-1", 4102444800.0
        self.tokens_changed = True
        return {}

    async def validate_session(self):
        return self.me.get("permalink")

    async def get_me(self):
        return self.me

    async def get_follower_count(self):
        return self.me.get("followers_count")

    async def get_my_tracks(self):
        return list(self.tracks)

    parse_track = staticmethod(ScClient.parse_track)

    async def upload_track(self, **kw):
        self.uploads.append(kw)
        return {"success": True, "id": "777", "url": "https://soundcloud.com/samplehandle/new-track"}

    async def update_track(self, track_id, **kw):
        self.updates.append((track_id, kw))
        return {"success": True, "id": str(track_id), "url": "https://soundcloud.com/samplehandle/new-track"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    from posting import artwork_reader as ar
    from database import db as dbmod
    monkeypatch.setattr(ar, "get_artwork_archive_path", lambda: tmp_path / "art")
    (tmp_path / "art").mkdir()
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    (tmp_path / "data").mkdir()
    settings = {"sc_client_id": "cid", "sc_client_secret": "sec", "sc_refresh_token": "rt-0",
                "sc_access_token": "at-0", "sc_username": "samplehandle"}
    monkeypatch.setattr(config, "get_settings", lambda: dict(settings))
    saved = {}
    monkeypatch.setattr(config, "save_settings", lambda d: (settings.update(d), saved.update(d)))
    monkeypatch.setattr(config, "delete_settings_keys", lambda keys: [settings.pop(k, None) for k in keys])
    name = ar.create_artwork(title="Sample Track", image_filename="track.mp3", image_bytes=FAKE_MP3,
                             description="A tune.", rating="general", tags={"default": ["sample", "field recording"]},
                             thumbnail_filename="poster.png", thumbnail_bytes=_png(), media={"duration_s": 190.4})
    return {"piece": name, "settings": settings, "saved": saved, "tmp": tmp_path}


# ── the client's pure parts ──────────────────────────────────────────────────

def test_pkce_and_the_authorize_url_follow_the_guide():
    verifier, challenge = pkce_pair()
    assert 43 <= len(verifier) <= 128 and "=" not in challenge and re.fullmatch(r"[A-Za-z0-9_-]+", challenge)
    url = authorize_url("cid", "https://pp.example/api/sc/auth/callback", challenge, "st4te")
    assert url.startswith("https://secure.soundcloud.com/authorize?")
    for part in ("client_id=cid", "response_type=code", "code_challenge_method=S256", f"code_challenge={challenge}",
                 "state=st4te", "redirect_uri=https%3A%2F%2Fpp.example%2Fapi%2Fsc%2Fauth%2Fcallback"):
        assert part in url


def test_tag_list_quotes_multi_word_tags_and_parse_track_reads_both_count_names():
    assert tag_list(["night", "field recording", "", '"x"']) == 'night "field recording" x'
    t = ScClient.parse_track(TRACK)
    assert t["submission_id"] == "123456" and t["urn"].endswith("123456") and t["username"] == "samplehandle"
    assert (t["views"], t["favorites_count"], t["comments_count"], t["reposts_count"]) == (42, 7, 2, 1)
    assert t["duration_ms"] == 190400 and t["link"].startswith("https://soundcloud.com/")
    older = dict(TRACK); older.pop("likes_count"); older["favoritings_count"] = 9
    assert ScClient.parse_track(older)["favorites_count"] == 9
    # the urn alone still yields an id
    assert ScClient.parse_track({"urn": "soundcloud:tracks:55"})["submission_id"] == "55"


def test_the_client_uses_the_oauth_scheme_and_rotates_the_pair(monkeypatch):
    c = ScClient(client_id="cid", client_secret="sec", refresh_token="rt-0")
    assert c._headers()["Authorization"] == "OAuth " and c._headers()["Accept"].startswith("application/json")

    class R:
        status_code = 200
        def json(self):
            return {"access_token": "at-1", "refresh_token": "rt-1", "expires_in": 3600}

    class H:
        async def post(self, url, data=None, headers=None):
            assert url == "https://secure.soundcloud.com/oauth/token"
            assert data["grant_type"] == "refresh_token" and data["client_secret"] == "sec" and data["refresh_token"] == "rt-0"
            return R()

    monkeypatch.setattr(c, "_http", lambda: H())
    asyncio.run(c.refresh())
    assert (c.access_token, c.refresh_token, c.tokens_changed) == ("at-1", "rt-1", True) and c.expires_at > 0
    assert inspect.iscoroutinefunction(ScClient.get_follower_count)


# ── the poll cycle ───────────────────────────────────────────────────────────

def test_the_poll_cycle_writes_rows_snapshots_followers_and_the_rotated_pair(env, monkeypatch):
    from polling import sc_poller
    from database import sc_queries
    fake = FakeScClient(access_token="at-new", refresh_token="rt-new", expires_at=4102444800.0)
    monkeypatch.setattr(sc_poller, "_get_or_create_client", lambda creds: fake)
    monkeypatch.setattr(sc_poller, "capture_followers", _capture_recorder := _Recorder())
    from database import accounts as adb
    conn = get_connection()
    try:
        adb.ensure_accounts_table(conn)
        acct = adb.get_default_account_id(conn, "sc", create=True)
    finally:
        conn.close()
    fake.tokens_changed = True                 # the cycle's first call refreshed
    stats = asyncio.run(sc_poller.run_sc_poll_cycle(acct))
    assert stats == {"submissions_found": 1, "snapshots_inserted": 1}
    conn = get_connection()
    try:
        row = sc_queries.get_sc_submission(conn, "123456")
        assert row["title"] == "Sample Track" and row["views"] == 42 and row["genre"] == "Ambient" and row["account_id"] == acct
        assert len(sc_queries.get_sc_snapshots(conn, "123456")) == 1
        assert sc_queries.get_sc_last_poll(conn)["status"] == "success"
        assert sc_queries.get_sc_summary(conn)["total_views"] == 42
    finally:
        conn.close()
    assert _capture_recorder.calls and _capture_recorder.calls[0][1] == acct
    # the rotated pair went to the default account's bare keys
    assert env["settings"]["sc_refresh_token"] == "rt-new" and env["settings"]["sc_access_token"] == "at-new"
    assert env["settings"]["sc_token_expires_at"] == "4102444800"


class _Recorder:
    def __init__(self):
        self.calls = []

    async def __call__(self, client, account_id, conn):
        self.calls.append((client, account_id))
        return True


# ── the routes ───────────────────────────────────────────────────────────────

@pytest.fixture
def api(env):
    from routes import sc_api
    app = FastAPI()
    app.include_router(sc_api.sc_router)
    return TestClient(app)


def test_connect_hands_back_the_authorize_url_and_the_callback_stores_the_pair(api, env, monkeypatch):
    from routes import sc_api
    r = api.post("/api/sc/auth/connect", json={"client_id": "cid2", "client_secret": "sec2"})
    assert r.status_code == 200, r.text
    info = r.json()
    assert info["url"].startswith("https://secure.soundcloud.com/authorize?") and info["state"] in sc_api._sc_oauth_state
    assert info["redirect_uri"] == "http://testserver/api/sc/auth/callback"
    assert env["settings"]["sc_client_id"] == "cid2" and env["settings"]["sc_client_secret"] == "sec2"
    assert api.post("/api/sc/auth/connect", json={"client_id": "", "client_secret": ""}).status_code == 200  # keeps the saved ones

    monkeypatch.setattr(scc, "ScClient", FakeScClient)
    # a foreign or expired state is refused before any exchange
    bad = api.get("/api/sc/auth/callback", params={"code": "good-code", "state": "nope"})
    assert bad.status_code == 400 and "did not come from this install" in bad.text
    # the refusal branch escapes what SoundCloud (or anyone) put in the query
    ref = api.get("/api/sc/auth/callback", params={"error": "access_denied", "error_description": "<b>x</b>"})
    assert ref.status_code == 400 and "<b>" not in ref.text and "&lt;b&gt;" in ref.text

    # the wrong SoundCloud account approving it is refused and nothing is saved
    env["settings"]["sc_username"] = "someoneelse"
    before = dict(env["settings"])
    r = api.get("/api/sc/auth/callback", params={"code": "good-code", "state": info["state"]})
    assert r.status_code == 400 and "Wrong SoundCloud account" in r.text
    assert env["settings"] == before and info["state"] not in sc_api._sc_oauth_state      # single-use

    env["settings"]["sc_username"] = "samplehandle"
    info = api.get("/api/sc/auth/authorize-url").json()
    r = api.get("/api/sc/auth/callback", params={"code": "good-code", "state": info["state"]})
    assert r.status_code == 200 and "SoundCloud connected" in r.text and "samplehandle" in r.text
    assert env["settings"]["sc_refresh_token"] == "rt-1" and env["settings"]["sc_access_token"] == "at-1"
    assert env["settings"]["sc_notifications_enabled"] is True
    from database import accounts as adb
    conn = get_connection()
    try:
        acct = adb.get_default_account_id(conn, "sc")
        assert acct is not None and adb.get_account(conn, acct)["handle"] == "samplehandle"
    finally:
        conn.close()

    st = api.get("/api/sc/auth/status").json()
    assert st["has_app"] and st["has_credentials"] and st["username"] == "samplehandle"
    assert api.post("/api/sc/auth/disconnect").status_code == 200
    assert "sc_refresh_token" not in env["settings"] and env["settings"]["sc_client_id"] == "cid2"
    assert api.get("/api/sc/auth/status").json()["has_credentials"] is False
    assert api.get("/api/sc/submissions/1/snapshots").json() == {"snapshots": []}


# ── the poster ───────────────────────────────────────────────────────────────

def _pkg(name, **over):
    from posting.platforms.base import StoryUploadPackage
    from posting import artwork_reader as ar
    art = ar.load_artwork(name)
    kw = dict(story_name=name, chapter_index=0, chapter_title="", platform="sc", title="Sample Track",
              description="A tune.", tags=["sample", "field recording"], rating="general", file_path=art.image_path,
              file_type="mp3", thumbnail_path=art.thumbnail_path, media_kind="audio", duration_s=190.4, extra={})
    kw.update(over)
    return StoryUploadPackage(**kw)


def test_the_poster_posts_edits_and_refuses_honestly(env, monkeypatch):
    from posting.platforms.soundcloud import SoundCloudPoster
    from polling import sc_poller
    fake = FakeScClient(refresh_token="rt-0")
    monkeypatch.setattr(sc_poller, "client_from_creds", lambda creds: fake)
    p = SoundCloudPoster()
    assert p.validate(_pkg(env["piece"])) == []
    res = asyncio.run(p.post(_pkg(env["piece"], extra={"sc_genre": "Ambient", "private": True})))
    assert res.success and res.external_id == "777" and res.external_url.startswith("https://soundcloud.com/")
    up = fake.uploads[0]
    assert up["title"] == "Sample Track" and up["sharing"] == "private" and up["genre"] == "Ambient"
    assert up["tags"] == ["sample", "field recording"] and up["artwork_path"] is None or Path(up["artwork_path"]).suffix == ".jpg"
    # the pair SoundCloud rotated on the way in was written back before the upload
    assert env["settings"]["sc_refresh_token"] == "rt-new"

    res = asyncio.run(p.edit("777", _pkg(env["piece"], title="Renamed")))
    assert res.success and fake.updates[0][0] == "777" and fake.updates[0][1]["title"] == "Renamed"
    assert not asyncio.run(p.replace_file("777", "x.mp3")).success

    # the gates: adult is refused (mature passes), video is refused, no token says so
    assert p.rating_refusal(_pkg(env["piece"], rating="adult")).startswith("SoundCloud doesn't take adult work")
    assert p.rating_refusal(_pkg(env["piece"], rating="mature")) is None
    assert "video" in p.media_refusal(_pkg(env["piece"], file_type="mp4", media_kind="video"))
    assert any("adult" in e for e in p.validate(_pkg(env["piece"], rating="adult")))
    env["settings"].pop("sc_refresh_token")
    assert any("not authorised" in e for e in p.validate(_pkg(env["piece"])))
    env["settings"].pop("sc_client_id")
    assert any("not configured" in e for e in p.validate(_pkg(env["piece"])))


# ── the registries ───────────────────────────────────────────────────────────

def test_soundcloud_is_registered_everywhere():
    from database import accounts as adb, platform_metrics, followers, analytics_queries
    from polling import session_check
    from polling.multi_account import get_poll_cycles
    from mirror import registry as mreg
    from routes import api as api_mod
    assert "sc" in adb.PLATFORMS and adb.PLATFORM_NAMES["sc"] == "SoundCloud" and "sc" not in adb.POST_ONLY_PLATFORMS
    assert adb.DEFAULT_CRED_CHECKS["sc"]({"sc_refresh_token": "x"}) and not adb.DEFAULT_CRED_CHECKS["sc"]({})
    assert adb._HANDLE_KEYS["sc"] == ["sc_username"]
    assert "sc_client_secret" in config.CREDENTIAL_FIELDS and "sc_client_id" not in config.CREDENTIAL_FIELDS
    assert config.PLATFORM_CREDENTIAL_FIELDS["sc"][:2] == ["sc_client_id", "sc_client_secret"]
    assert platform_metrics.get("sc").table == "sc_submissions" and "sc" in followers.FOLLOWER_PLATFORMS
    assert analytics_queries.INSIGHT_TABLES["sc"] == "sc_submissions"
    assert "sc" in session_check.CHECKABLE and session_check.LABELS["sc"] == "SoundCloud"
    assert "sc" in get_poll_cycles() and "sc_" in mreg.PLATFORM_PREFIXES
    assert any(c[0] == "sc" for c in api_mod._PLATFORM_HEALTH_CONFIG)
    from posting.manager import _get_poster
    assert _get_poster("sc").platform_name == "SoundCloud"
    root = Path(__file__).resolve().parent.parent
    pj = (root / "frontend" / "js" / "platforms.js").read_text(encoding="utf-8")
    assert "code: 'sc'" in pj and "sc:   V()," in pj
    app_js = (root / "frontend" / "js" / "app.js").read_text(encoding="utf-8")
    for needle in ("renderSCDashboard", "data-platform=\"sc\"", "sc-connect-btn", "tableFn: 'scPollLogTable'"):
        assert needle in app_js, needle
    comp = (root / "frontend" / "js" / "components.js").read_text(encoding="utf-8")
    assert "scPollLogTable(polls)" in comp and "scTopList(" in comp
    api_js = (root / "frontend" / "js" / "api.js").read_text(encoding="utf-8")
    assert "scConnect(data)" in api_js and "getSCAuthorizeUrl" in api_js
