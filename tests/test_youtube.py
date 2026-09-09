"""YouTube (`yt`) — MEDIAPLATS §6 (4.24.0), built against the documented shapes with a fake client.

The client's pure parts (the consent URL with offline access, the video parser), the resumable
upload protocol against a fake HTTP layer (session → 308 with Range → 200), the poll cycle end
to end with a fake client (uploads playlist → statistics → rows + the subscriber series), the
OAuth routes (connect → callback storing the pair + the channel's long-uploads status), the
poster (private-until-audited reporting, the thumbnail, the mature ceiling) and the registries.
No network anywhere.
"""
from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import config
from clients.yt import client as ytc
from clients.yt.client import SCOPES, YtClient, authorize_url, pkce_pair
from database.db import get_connection


def _png(w=64, h=36) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (200, 90, 120)).save(buf, format="PNG")
    return buf.getvalue()


FAKE_MP4 = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * 10000   # > one 4096-byte test chunk

VIDEO = {
    "id": "abc123XYZ", "snippet": {"title": "Sample Clip", "description": "A clip.", "tags": ["sample", "clip"], "categoryId": "1",
                                   "channelTitle": "Sample Channel", "publishedAt": "2026-09-01T10:00:00Z",
                                   "thumbnails": {"default": {"url": "https://i.ytimg.com/vi/abc123XYZ/default.jpg"},
                                                  "high": {"url": "https://i.ytimg.com/vi/abc123XYZ/hqdefault.jpg"}}},
    "statistics": {"viewCount": "42", "likeCount": "7", "commentCount": "2"},
    "status": {"privacyStatus": "private", "uploadStatus": "processed"},
    "contentDetails": {"duration": "PT3M10S"},
}
CHANNEL = {"id": "UCxyz", "title": "Sample Channel", "handle": "@samplechannel", "uploads": "UUxyz",
           "subscribers": 12, "videos": 1, "long_uploads": "allowed"}


class FakeYtClient:
    def __init__(self, client_id="", client_secret="", access_token="", refresh_token="", expires_at=0.0):
        self.client_id, self.client_secret = client_id, client_secret
        self.access_token, self.refresh_token, self.expires_at = access_token, refresh_token, expires_at
        self.tokens_changed = False
        self.channel = dict(CHANNEL)
        self.videos = [YtClient.parse_video(VIDEO)]
        self.uploads: list[dict] = []
        self.thumbs: list[tuple] = []
        self.updates: list[tuple] = []
        self.upload_privacy = "private"           # what YouTube actually sets (an unaudited project)

    async def close(self):
        pass

    async def refresh(self):
        self.access_token, self.expires_at, self.tokens_changed = "at-new", 4102444800.0, True
        return {}

    async def exchange_code(self, code, redirect_uri, verifier):
        assert code == "good-code" and verifier
        self.access_token, self.refresh_token, self.expires_at, self.tokens_changed = "at-1", "rt-1", 4102444800.0, True
        return {}

    async def get_channel(self):
        return self.channel

    async def validate_session(self):
        return self.channel.get("handle")

    async def get_follower_count(self):
        return self.channel.get("subscribers")

    async def get_my_video_ids(self):
        return [v["submission_id"] for v in self.videos]

    async def get_videos(self, ids):
        return [v for v in self.videos if v["submission_id"] in ids]

    async def upload_video(self, **kw):
        self.uploads.append(kw)
        return {"success": True, "id": "newvid01", "url": "https://www.youtube.com/watch?v=newvid01", "privacy": self.upload_privacy}

    async def set_thumbnail(self, video_id, image_path):
        self.thumbs.append((video_id, image_path))
        return {"success": True}

    async def update_video(self, video_id, **kw):
        self.updates.append((video_id, kw))
        return {"success": True, "id": video_id, "url": f"https://www.youtube.com/watch?v={video_id}"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    from posting import artwork_reader as ar
    monkeypatch.setattr(ar, "get_artwork_archive_path", lambda: tmp_path / "art")
    (tmp_path / "art").mkdir()
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    (tmp_path / "data").mkdir()
    settings = {"yt_client_id": "cid", "yt_client_secret": "sec", "yt_refresh_token": "rt-0",
                "yt_access_token": "at-0", "yt_username": "@samplechannel"}
    monkeypatch.setattr(config, "get_settings", lambda: dict(settings))
    saved = {}
    monkeypatch.setattr(config, "save_settings", lambda d: (settings.update(d), saved.update(d)))
    monkeypatch.setattr(config, "delete_settings_keys", lambda keys: [settings.pop(k, None) for k in keys])
    name = ar.create_artwork(title="Sample Clip", image_filename="clip.mp4", image_bytes=FAKE_MP4,
                             description="A clip.", rating="general", tags={"default": ["sample", "clip"]},
                             thumbnail_filename="poster.png", thumbnail_bytes=_png(640, 360),
                             media={"duration_s": 190.0, "width": 640, "height": 360})
    return {"piece": name, "settings": settings, "saved": saved, "tmp": tmp_path}


# ── the client's pure parts ──────────────────────────────────────────────────

def test_the_consent_url_asks_for_offline_access_and_the_parser_reads_statistics():
    verifier, challenge = pkce_pair()
    url = authorize_url("cid", "https://pp.example/api/yt/auth/callback", challenge, "st4te")
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    for part in ("access_type=offline", "prompt=consent", "code_challenge_method=S256", "state=st4te",
                 "scope=" + "+".join(s.replace("https://www.googleapis.com/auth/", "https%3A%2F%2Fwww.googleapis.com%2Fauth%2F") for s in SCOPES)):
        assert part in url, part
    v = YtClient.parse_video(VIDEO)
    assert v["submission_id"] == "abc123XYZ" and (v["views"], v["favorites_count"], v["comments_count"]) == (42, 7, 2)
    assert v["privacy"] == "private" and v["duration"] == "PT3M10S" and v["thumbnail_url"].endswith("hqdefault.jpg")
    assert v["link"] == "https://www.youtube.com/watch?v=abc123XYZ" and v["tags"] == ["sample", "clip"]


def test_the_resumable_upload_follows_the_protocol(tmp_path, monkeypatch):
    video = tmp_path / "v.mp4"; video.write_bytes(FAKE_MP4)
    c = YtClient(client_id="cid", client_secret="sec", access_token="at", refresh_token="rt", expires_at=4102444800.0)
    puts: list[dict] = []

    class R:
        def __init__(self, status, headers=None, body=None):
            self.status_code, self.headers, self._body = status, headers or {}, body
            self.text = json.dumps(body) if body else ""

        def json(self):
            if self._body is None:
                raise ValueError
            return self._body

    class H:
        async def request(self, method, url, headers=None, **kw):
            assert method == "POST" and url.endswith("/upload/youtube/v3/videos")
            assert kw["params"]["uploadType"] == "resumable" and headers["X-Upload-Content-Length"] == str(len(FAKE_MP4))
            meta = json.loads(kw["content"])
            assert meta["snippet"]["title"] == "Sample Clip" and meta["status"]["privacyStatus"] == "unlisted"
            return R(200, {"location": "https://upload.example/session/1"})

    class Up:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def put(self, url, content=None, headers=None):
            puts.append({"range": headers["Content-Range"], "len": len(content)})
            # the first chunk is "interrupted" after 1000 bytes: 308 + Range says what arrived
            if len(puts) == 1:
                return R(308, {"range": "bytes=0-999"})
            return R(200, {}, {"id": "newvid01", "snippet": {"title": "Sample Clip"}, "status": {"privacyStatus": "private"}})

    monkeypatch.setattr(c, "_http", lambda: H())
    monkeypatch.setattr(ytc, "CHUNK", 4096)
    monkeypatch.setattr(ytc.httpx, "AsyncClient", Up)
    r = asyncio.run(c.upload_video(file_path=str(video), title="Sample Clip", privacy="unlisted"))
    assert r["success"] and r["id"] == "newvid01" and r["privacy"] == "private"        # what YouTube set, not what was asked
    assert puts[0]["range"] == f"bytes 0-4095/{len(FAKE_MP4)}" and puts[1]["range"].startswith("bytes 1000-")


# ── the poll cycle ───────────────────────────────────────────────────────────

def test_the_poll_cycle_writes_rows_and_the_subscriber_series(env, monkeypatch):
    from polling import yt_poller
    from database import yt_queries, accounts as adb
    fake = FakeYtClient(access_token="at-new", refresh_token="rt-0", expires_at=4102444800.0)
    fake.tokens_changed = True
    monkeypatch.setattr(yt_poller, "_get_or_create_client", lambda creds: fake)
    calls = []

    async def rec(client, account_id, conn):
        calls.append(account_id); return True
    monkeypatch.setattr(yt_poller, "capture_followers", rec)
    conn = get_connection()
    try:
        adb.ensure_accounts_table(conn)
        acct = adb.get_default_account_id(conn, "yt", create=True)
    finally:
        conn.close()
    stats = asyncio.run(yt_poller.run_yt_poll_cycle(acct))
    assert stats == {"submissions_found": 1, "snapshots_inserted": 1} and calls == [acct]
    conn = get_connection()
    try:
        row = yt_queries.get_yt_submission(conn, "abc123XYZ")
        assert row["title"] == "Sample Clip" and row["views"] == 42 and row["privacy"] == "private" and row["tags"] == "sample, clip"
        assert yt_queries.get_yt_last_poll(conn)["status"] == "success"
    finally:
        conn.close()
    assert env["settings"]["yt_access_token"] == "at-new"


# ── the routes ───────────────────────────────────────────────────────────────

@pytest.fixture
def api(env):
    from routes import yt_api
    app = FastAPI()
    app.include_router(yt_api.yt_router)
    return TestClient(app)


def test_connect_and_the_callback_store_the_pair_and_the_channel(api, env, monkeypatch):
    from routes import yt_api
    r = api.post("/api/yt/auth/connect", json={"client_id": "cid2", "client_secret": "sec2"})
    assert r.status_code == 200, r.text
    info = r.json()
    assert info["url"].startswith("https://accounts.google.com/o/oauth2/v2/auth?") and "access_type=offline" in info["url"]
    assert info["redirect_uri"] == "http://testserver/api/yt/auth/callback" and info["state"] in yt_api._yt_oauth_state
    monkeypatch.setattr(ytc, "YtClient", FakeYtClient)
    assert api.get("/api/yt/auth/callback", params={"code": "good-code", "state": "nope"}).status_code == 400
    env["settings"]["yt_username"] = "@someoneelse"
    r = api.get("/api/yt/auth/callback", params={"code": "good-code", "state": info["state"]})
    assert r.status_code == 400 and "Wrong YouTube channel" in r.text
    env["settings"]["yt_username"] = "@samplechannel"
    info = api.get("/api/yt/auth/authorize-url").json()
    r = api.get("/api/yt/auth/callback", params={"code": "good-code", "state": info["state"]})
    assert r.status_code == 200 and "stay private" in r.text
    assert env["settings"]["yt_refresh_token"] == "rt-1" and env["settings"]["yt_long_uploads"] == "allowed"
    st = api.get("/api/yt/auth/status").json()
    assert st["has_credentials"] and st["username"] == "@samplechannel" and st["long_uploads"] == "allowed" and "audit" in st["audit_note"]
    assert api.get("/api/yt/submissions/1/snapshots").json() == {"snapshots": []}
    assert api.post("/api/yt/auth/disconnect").status_code == 200
    assert "yt_refresh_token" not in env["settings"] and env["settings"]["yt_client_id"] == "cid2"


# ── the poster ───────────────────────────────────────────────────────────────

def _pkg(name, **over):
    from posting.platforms.base import StoryUploadPackage
    from posting import artwork_reader as ar
    art = ar.load_artwork(name)
    kw = dict(story_name=name, chapter_index=0, chapter_title="", platform="yt", title="Sample Clip",
              description="A clip.", tags=["sample", "clip"], rating="general", file_path=art.image_path,
              file_type="mp4", thumbnail_path=art.thumbnail_path, media_kind="video", duration_s=190.0, extra={})
    kw.update(over)
    return StoryUploadPackage(**kw)


def test_the_poster_uploads_reports_privacy_honestly_and_refuses_adult(env, monkeypatch):
    from posting.platforms.youtube import YouTubePoster
    from polling import yt_poller
    fake = FakeYtClient(refresh_token="rt-0", access_token="at-0", expires_at=4102444800.0)
    monkeypatch.setattr(yt_poller, "client_from_creds", lambda creds: fake)
    p = YouTubePoster()
    assert p.validate(_pkg(env["piece"])) == []
    res = asyncio.run(p.post(_pkg(env["piece"], extra={"yt_privacy": "public", "yt_category": "10"})))
    assert res.success and res.external_id == "newvid01"
    assert "watch?v=newvid01" in res.external_url and "unaudited project" in res.external_url    # asked public, got private
    up = fake.uploads[0]
    assert up["privacy"] == "public" and up["category_id"] == "10" and up["mime"] == "video/mp4" and up["tags"] == ["sample", "clip"]
    assert fake.thumbs and fake.thumbs[0][0] == "newvid01" and fake.thumbs[0][1].endswith(".jpg")
    fake.upload_privacy = "public"
    res = asyncio.run(p.post(_pkg(env["piece"], extra={"yt_privacy": "public"})))
    assert res.success and res.external_url == "https://www.youtube.com/watch?v=newvid01"
    res = asyncio.run(p.edit("newvid01", _pkg(env["piece"], title="Renamed", extra={"yt_privacy": "unlisted"})))
    assert res.success and fake.updates[0][0] == "newvid01" and fake.updates[0][1]["privacy"] == "unlisted"
    assert not asyncio.run(p.replace_file("newvid01", "x")).success
    assert p.rating_refusal(_pkg(env["piece"], rating="adult")).startswith("YouTube doesn't take adult work")
    assert p.rating_refusal(_pkg(env["piece"], rating="mature")) is None
    assert "audio" in p.media_refusal(_pkg(env["piece"], file_type="mp3", media_kind="audio"))
    env["settings"].pop("yt_refresh_token")
    assert any("not authorised" in e for e in p.validate(_pkg(env["piece"])))


# ── the registries ───────────────────────────────────────────────────────────

def test_youtube_is_registered_everywhere():
    from database import accounts as adb, platform_metrics, followers, analytics_queries
    from polling import session_check
    from polling.multi_account import get_poll_cycles
    from mirror import registry as mreg
    from routes import api as api_mod
    assert "yt" in adb.PLATFORMS and adb.PLATFORM_NAMES["yt"] == "YouTube" and "yt" not in adb.POST_ONLY_PLATFORMS
    assert adb.DEFAULT_CRED_CHECKS["yt"]({"yt_refresh_token": "x"}) and adb._HANDLE_KEYS["yt"] == ["yt_username"]
    assert "yt_client_secret" in config.CREDENTIAL_FIELDS and "yt_client_id" not in config.CREDENTIAL_FIELDS
    assert config.PLATFORM_CREDENTIAL_FIELDS["yt"][:2] == ["yt_client_id", "yt_client_secret"]
    assert platform_metrics.get("yt").label_for("faves") == "Likes" and "yt" in followers.FOLLOWER_PLATFORMS
    assert analytics_queries.INSIGHT_TABLES["yt"] == "yt_submissions"
    assert "yt" in session_check.CHECKABLE and "yt" in get_poll_cycles() and "yt_" in mreg.PLATFORM_PREFIXES
    assert any(c[0] == "yt" for c in api_mod._PLATFORM_HEALTH_CONFIG)
    from posting.manager import _get_poster
    assert _get_poster("yt").max_rating == "mature"
    root = Path(__file__).resolve().parent.parent
    pj = (root / "frontend" / "js" / "platforms.js").read_text(encoding="utf-8")
    assert "code: 'yt'" in pj and "yt:   V({ faves: 'Likes' })," in pj
    app_js = (root / "frontend" / "js" / "app.js").read_text(encoding="utf-8")
    for needle in ("renderYTDashboard", "data-platform=\"yt\"", "yt-connect-btn", "tableFn: 'ytPollLogTable'", "stay <strong>private</strong>"):
        assert needle in app_js, needle
    comp = (root / "frontend" / "js" / "components.js").read_text(encoding="utf-8")
    assert "ytPollLogTable(polls)" in comp and "ytTopList(" in comp
    api_js = (root / "frontend" / "js" / "api.js").read_text(encoding="utf-8")
    assert "ytConnect(data)" in api_js and "getYTAuthorizeUrl" in api_js
