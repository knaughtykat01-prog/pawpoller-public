"""Newgrounds (`ng`) — MEDIAPLATS §5 (4.23.0), scrape + cookie-and-form, no network.

The parsers are pinned to the markup read from the live public pages on 2026-09-08 (a user's
audio / movies listings, an item's ``#sidestats`` block, the profile's FANS count, the rated
title), the project flow runs against a fake HTTP layer that answers the documented JSON, the
poll cycle and the routes run against a fake client, and the registries are checked.
"""
from __future__ import annotations

import asyncio
import io
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import config
from clients.ng import client as ngc
from clients.ng.client import (NgClient, descriptors_for, logged_in_username, ng_tags, parse_fans, parse_item,
                               parse_listing, portal_for, same_ng_user)
from database.db import get_connection


def _png(w=64, h=36) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (200, 90, 120)).save(buf, format="PNG")
    return buf.getvalue()


FAKE_MP3 = b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\xff\xfb" * 100

LISTING_AUDIO = '''
<a href="https://www.newgrounds.com/audio/listen/254533"><strong class="highlight">Highlight: </strong><span>Caves</span></a>
<a href="https://www.newgrounds.com/audio/listen/254533" class="item-audiosubmission " title="Glittering Caves">
  <div class="item-icon"><div><img src="https://img.ngfiles.com/defaults/icon-audio-smaller.png" alt="Glittering Caves"></div></div></a>
<a href="https://www.newgrounds.com/audio/listen/6499" class="item-audiosubmission " title="Boss music &amp;1"><div></div></a>
'''
LISTING_MOVIE = '''
<a href="https://www.newgrounds.com/portal/view/405919" class="inline-card-portalsubmission" title="Sample Five">
<img class="card-img" src="https://picon.ngfiles.com/405000/flash_405919_card.png" alt="Sample Five"><div class="card-title"><h4>Sample Five</h4></div>
<div class="rating"><div class="nohue-ngicon-small-rated-m"></div></div><div class="star-score" title="Score: 4.55/5.00"></div></a>
'''
ITEM_AUDIO = '''
<meta property="og:title" content="Chilled Sample"><meta property="og:description" content="A nice laid back tune"><meta property="og:image" content="https://img.ngfiles.com/defaults/icon-audio-xl.webp">
<h2 class="rated-e" itemprop="name">Chilled Sample</h2>
<div id="sidestats">
  <dl class="sidestats single-row" data-statistics="1_3">
    <dt>Listens</dt><dd>302,940</dd>
    <dt>Faves</dt><dd><a href="https://www.newgrounds.com/favorites/content/who/1/3" id="faves_load">400</a><script>x</script></dd>
    <dt>Downloads</dt><dd>80,461</dd>
    <dt>Votes</dt><dd>2,521</dd>
    <dt>Score</dt><dd><div><div><span id="score_number">4.45</span> / 5.00 <span id="submission_score_change"></span></div></div></dd>
  </dl><hr>
  <dl class="sidestats"><dt class="no-margin">Uploaded</dt><dd class="multivalue"><span class="value">Feb 13, 2003</span><span class="value">9:35 AM EST</span></dd></dl>
  <div class="flexbox align-center" id="genre-view-x"><dl class="sidestats flex-1"><dt>Genre</dt><dd><a href="https://www.newgrounds.com/audio/browse?genre=10" data-genre-for="210804">Techno</a></dd></dl></div>
</div>
'''
ITEM_MOVIE = ITEM_AUDIO.replace("Listens", "Views").replace("rated-e", "rated-m").replace('<dt>Downloads</dt><dd>80,461</dd>', "")
PROFILE = '<div><span>FANS</span><strong>562</strong></div>'
SIGNED_IN_HOME = '<script>PHP.merge({"activeuser":1,"name":"samplehandle","uek":"k3y"});</script><div class="activeuser">'


# ── the parsers ──────────────────────────────────────────────────────────────

def test_the_listing_parsers_read_ids_and_titles_once_each():
    a = parse_listing(LISTING_AUDIO, "audio")
    assert a == [{"submission_id": "254533", "title": "Glittering Caves"}, {"submission_id": "6499", "title": "Boss music &1"}]
    assert parse_listing(LISTING_MOVIE, "movie") == [{"submission_id": "405919", "title": "Sample Five"}]
    assert parse_listing(LISTING_AUDIO, "movie") == []


def test_the_item_parser_reads_the_sidestats_block_and_the_rated_title():
    d = parse_item(ITEM_AUDIO, "1", "audio", "samplehandle")
    assert d["title"] == "Chilled Sample" and d["rating"] == "e" and d["genre"] == "Techno"
    assert (d["views"], d["favorites_count"], d["downloads_count"], d["votes"], d["score"]) == (302940, 400, 80461, 2521, 4.45)
    assert d["posted_at"] == "Feb 13, 2003 9:35 AM EST" and d["link"] == "https://www.newgrounds.com/audio/listen/1"
    assert d["thumbnail_url"].startswith("https://img.ngfiles.com/") and d["description"] == "A nice laid back tune"
    m = parse_item(ITEM_MOVIE, "405919", "movie")
    assert m["views"] == 302940 and m["rating"] == "m" and m["downloads_count"] == 0 and m["portal"] == "movie"
    assert m["link"] == "https://www.newgrounds.com/portal/view/405919"
    assert parse_fans(PROFILE) == 562 and parse_fans("<div>nothing</div>") is None


def test_session_identity_tags_descriptors_and_portals():
    assert logged_in_username(SIGNED_IN_HOME) == "samplehandle" and logged_in_username("<html>Login / Sign Up</html>") == ""
    assert same_ng_user("Sample-Handle", "samplehandle") and not same_ng_user("", "x")
    assert ng_tags(["Field Recording", "night (live)", "it's", "night-live", "a:b"]) == ["field-recording", "night-live", "its", "ab"]
    assert len(ng_tags([f"t{i}" for i in range(20)])) == 12
    assert descriptors_for("general") == {"nudity": "c", "violence": "c", "language_textual": "c", "adult_themes": "c"}
    assert descriptors_for("adult")["nudity"] == "a" and descriptors_for("mature", {"ng_violence": "a"})["violence"] == "a"
    assert portal_for("audio") == "audio" and portal_for("video") == "movie" and portal_for("", "mp3") == "audio"
    assert portal_for("", "mov") == "movie" and portal_for("image", "png") == "art" and portal_for("", "webp") == "art"
    assert portal_for("", "wav") is None and portal_for("", "webm") is None       # not on the site's submit menu


# ── the project flow against a fake HTTP layer ───────────────────────────────

class FakeResp:
    def __init__(self, status=200, text="", json=None, url=""):
        self.status_code, self.text, self._json, self.url = status, text, json, url

    def json(self):
        if self._json is None:
            raise ValueError("not json")
        return self._json


class FakeHttp:
    """Answers the project flow the way PostyBirb's captures say the art portal does."""

    def __init__(self, fail_field: str | None = None):
        self.calls: list[tuple] = []
        self.fail_field = fail_field

    async def get(self, url, **kw):
        self.calls.append(("GET", url))
        if url.endswith("/new") or url.endswith("/edit"):
            return FakeResp(text='<script>PHP.merge({"uek":"k3y"})</script>')
        if url == "https://www.newgrounds.com/":
            return FakeResp(text=SIGNED_IN_HOME)
        return FakeResp(status=404)

    async def post(self, url, data=None, files=None, headers=None, timeout=None):
        self.calls.append(("POST", url, dict(data or {}), sorted((files or {}).keys())))
        if url.endswith("/new"):
            return FakeResp(json={"project_id": 987, "edit_url": "/projects/audio/987/edit", "success": "saved", "can_publish": False})
        if url.endswith("/remove/987"):
            return FakeResp(json={"success": "removed"})
        if url.endswith("/publish"):
            return FakeResp(text="ok", url="https://www.newgrounds.com/audio/listen/987")
        # the edit page: one field per request
        if self.fail_field and self.fail_field in (data or {}):
            return FakeResp(json={"success": "saved", f"{self.fail_field}_error": "MP3 must be sampled at 44.1 kHz"})
        body = {"success": "saved", "can_publish": True}
        if files:
            body["linked_icon"] = 1
        return FakeResp(json=body)


def test_the_project_flow_posts_uploads_fields_and_publishes(tmp_path):
    audio = tmp_path / "t.mp3"; audio.write_bytes(FAKE_MP3)
    icon = tmp_path / "i.jpg"; icon.write_bytes(_png())
    c = NgClient(username="samplehandle", cookie="a=1; b=2")
    http = FakeHttp()
    c._http = lambda: http
    r = asyncio.run(c.submit_project("audio", file_path=str(audio), title="Chilled Sample", description="A tune.\n\nSecond.",
                                     tags=["field recording", "night"], genre_id="10", rating="mature",
                                     icon_path=str(icon), extra={"ng_adult_themes": "a"}))
    assert r == {"success": True, "id": "987", "url": "https://www.newgrounds.com/audio/listen/987", "under_judgment": True}
    posts = [x for x in http.calls if x[0] == "POST"]
    assert posts[0][1].endswith("/projects/audio/new") and posts[0][2]["init_project"] == "1" and posts[0][2]["userkey"] == "k3y"
    assert posts[1][3] == ["new_audio", "thumbnail"]                                  # the file + the icon
    assert posts[2][2]["option[longdescription]"] == "<p>A tune.</p><p>Second.</p>" and posts[2][2]["encoder"] == "quill"
    sent = {}
    for x in posts[3:-1]:
        sent.update({k: v for k, v in x[2].items() if k not in ("userkey", "PHP_SESSION_UPLOAD_PROGRESS")})
    assert sent["title"] == "Chilled Sample" and sent["option[tags]"] == "field-recording,night" and sent["option[genreid]"] == "10"
    assert sent["option[nudity]"] == "b" and sent["option[adult_themes]"] == "a" and sent["option[include_in_portal]"] == "1"
    assert posts[-1][1].endswith("/projects/audio/987/publish") and posts[-1][2]["agree"] == "Y"
    assert all(len(x) == 4 and x[2].get("userkey") == "k3y" for x in posts)

    # the site refusing a field: the draft is removed and its own words come back
    http2 = FakeHttp(fail_field="title")
    c._http = lambda: http2
    r = asyncio.run(c.submit_project("audio", file_path=str(audio), title="x", rating="general"))
    assert not r["success"] and "44.1 kHz" in r["error"]
    assert any(x[1].endswith("/projects/audio/remove/987") for x in http2.calls if x[0] == "POST")

    # the movie portal uses its own paths and file field
    http3 = FakeHttp()
    c._http = lambda: http3
    video = tmp_path / "v.mp4"; video.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64)
    r = asyncio.run(c.submit_project("movie", file_path=str(video), title="Clip", rating="general"))
    assert r["success"] and http3.calls[0][1].endswith("/projects/movies/new")
    assert [x for x in http3.calls if x[0] == "POST"][1][3] == ["new_movie"]

    # the art portal (4.29.0, PostyBirb's proven flow): size + crop + link_icon on the upload,
    # then the returned linked_icon sorted in, then the same fields, then publish
    http4 = FakeHttp()
    c._http = lambda: http4
    image = tmp_path / "a.png"; image.write_bytes(_png())
    r = asyncio.run(c.submit_project("art", file_path=str(image), title="Sketch", rating="general", icon_path=str(icon)))
    assert r["success"] and http4.calls[0][1].endswith("/projects/art/new")
    posts4 = [x for x in http4.calls if x[0] == "POST"]
    up = posts4[1]
    assert up[3] == ["new_image", "thumbnail"] and up[2]["link_icon"] == "1"
    assert int(up[2]["width"]) > 0 and int(up[2]["height"]) > 0 and up[2]["cropdata"].startswith('{"x":0,"y":0,')
    assert posts4[2][2]["art_image_sort"] == "[1]"
    assert posts4[-1][1].endswith("/projects/art/987/publish")

    # session identity
    s = asyncio.run(c.validate_session())
    assert s["ok"] and s["username"] == "samplehandle"
    wrong = NgClient(username="someoneelse", cookie="a=1"); wrong._http = lambda: http3
    s = asyncio.run(wrong.validate_session())
    assert s["logged_in"] and not s["ok"] and "samplehandle" in s["detail"]


# ── the poll cycle + routes with a fake client ───────────────────────────────

class FakeNgClient:
    def __init__(self, username="samplehandle", cookie="a=1"):
        self.username, self.cookie = username, cookie
        self.session = {"ok": True, "logged_in": True, "username": "samplehandle", "expected": username, "matches": True, "detail": ""}

    async def close(self):
        pass

    async def validate_session(self):
        return self.session

    async def get_all_items(self, portal):
        if portal == "audio":
            return [{"submission_id": "1", "title": "Chilled Sample", "portal": "audio"}]
        if portal == "art":     # 4.29.0: an art id is its user/slug path
            return [{"submission_id": "samplehandle/sample-sketch", "title": "Sample Sketch", "portal": "art"}]
        return [{"submission_id": "405919", "title": "Sample Five", "portal": "movie"}]

    async def get_item(self, sid, portal):
        return parse_item(ITEM_AUDIO if portal == "audio" else ITEM_MOVIE, sid, portal, self.username)

    async def get_follower_count(self):
        return 562


@pytest.fixture
def env(tmp_path, monkeypatch):
    from posting import artwork_reader as ar
    monkeypatch.setattr(ar, "get_artwork_archive_path", lambda: tmp_path / "art")
    (tmp_path / "art").mkdir()
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    (tmp_path / "data").mkdir()
    settings = {"ng_username": "samplehandle", "ng_cookie": "a=1; b=2"}
    monkeypatch.setattr(config, "get_settings", lambda: dict(settings))
    monkeypatch.setattr(config, "save_settings", lambda d: settings.update(d))
    monkeypatch.setattr(config, "delete_settings_keys", lambda keys: [settings.pop(k, None) for k in keys])
    name = ar.create_artwork(title="Chilled Sample", image_filename="track.mp3", image_bytes=FAKE_MP3,
                             description="A tune.", rating="general", tags={"default": ["sample"]},
                             thumbnail_filename="poster.png", thumbnail_bytes=_png(), media={"duration_s": 190.4})
    return {"piece": name, "settings": settings, "tmp": tmp_path}


def test_the_poll_cycle_writes_both_portals_and_the_follower_series(env, monkeypatch):
    from polling import ng_poller
    from database import ng_queries
    from database import accounts as adb
    monkeypatch.setattr(ng_poller, "_get_or_create_client", lambda creds: FakeNgClient())
    monkeypatch.setattr(ng_poller, "ITEM_PAUSE_S", 0)
    calls = []

    async def rec(client, account_id, conn):
        calls.append(account_id); return True
    monkeypatch.setattr(ng_poller, "capture_followers", rec)
    conn = get_connection()
    try:
        adb.ensure_accounts_table(conn)
        acct = adb.get_default_account_id(conn, "ng", create=True)
    finally:
        conn.close()
    stats = asyncio.run(ng_poller.run_ng_poll_cycle(acct))
    assert stats == {"submissions_found": 3, "snapshots_inserted": 3} and calls == [acct]   # audio + movie + art (4.29.0)
    conn = get_connection()
    try:
        a = ng_queries.get_ng_submission(conn, "1")
        m = ng_queries.get_ng_submission(conn, "405919")
        assert a["portal"] == "audio" and a["views"] == 302940 and a["score"] == 4.45 and a["genre"] == "Techno"
        assert m["portal"] == "movie" and m["rating"] == "m" and m["account_id"] == acct
        assert ng_queries.get_ng_summary(conn)["total_submissions"] == 3
        assert ng_queries.get_ng_submission(conn, "samplehandle/sample-sketch")["portal"] == "art"
        snap = ng_queries.get_ng_snapshots(conn, "1")[0]
        assert (snap["score"], snap["votes"], snap["downloads_count"]) == (4.45, 2521, 80461)   # the judgment is a series
        assert ng_queries.get_ng_last_poll(conn)["status"] == "success"
    finally:
        conn.close()


@pytest.fixture
def api(env):
    from routes import ng_api
    app = FastAPI()
    app.include_router(ng_api.ng_router)
    return TestClient(app)


def test_connect_checks_whose_session_it_is(api, env, monkeypatch):
    fake = FakeNgClient()
    monkeypatch.setattr(ngc, "NgClient", lambda username="", cookie="": fake)
    env["settings"].clear()
    assert api.get("/api/ng/auth/status").json()["has_credentials"] is False
    assert api.post("/api/ng/auth/connect", json={"username": "x", "cookie": ""}).status_code == 400
    fake.session = {"ok": False, "logged_in": False, "username": "", "detail": "The cookie is not signed in."}
    assert api.post("/api/ng/auth/connect", json={"username": "samplehandle", "cookie": "a=1"}).status_code == 401
    fake.session = {"ok": False, "logged_in": True, "username": "someoneelse", "detail": "The session belongs to someoneelse, not samplehandle."}
    r = api.post("/api/ng/auth/connect", json={"username": "samplehandle", "cookie": "a=1"})
    assert r.status_code == 409 and "someoneelse" in r.json()["detail"] and "ng_cookie" not in env["settings"]
    fake.session = {"ok": True, "logged_in": True, "username": "samplehandle", "detail": ""}
    r = api.post("/api/ng/auth/connect", json={"username": "", "cookie": "a=1; b=2"})
    assert r.status_code == 200 and r.json()["username"] == "samplehandle"
    assert env["settings"]["ng_cookie"] == "a=1; b=2" and env["settings"]["ng_username"] == "samplehandle"
    st = api.get("/api/ng/auth/status").json()
    assert st["has_credentials"] and st["username"] == "samplehandle"
    assert api.get("/api/ng/submissions/1/snapshots").json() == {"snapshots": []}
    assert api.get("/api/ng/submissions", params={"portal": "audio"}).json()["total"] == 0
    assert api.post("/api/ng/auth/disconnect").status_code == 200 and "ng_cookie" not in env["settings"]


# ── the poster ───────────────────────────────────────────────────────────────

def _pkg(name, **over):
    from posting.platforms.base import StoryUploadPackage
    from posting import artwork_reader as ar
    art = ar.load_artwork(name)
    kw = dict(story_name=name, chapter_index=0, chapter_title="", platform="ng", title="Chilled Sample",
              description="A tune.", tags=["sample", "field recording"], rating="general", file_path=art.image_path,
              file_type="mp3", thumbnail_path=art.thumbnail_path, media_kind="audio", duration_s=190.4, extra={})
    kw.update(over)
    return StoryUploadPackage(**kw)


def test_the_poster_routes_by_portal_and_refuses_honestly(env, monkeypatch):
    from posting.platforms.newgrounds import NewgroundsPoster
    from posting.platforms import newgrounds as ngp

    class FakeClient(FakeNgClient):
        def __init__(self, username="", cookie=""):
            super().__init__(username, cookie)
            self.submitted, self.updated = [], []

        async def submit_project(self, portal, **kw):
            self.submitted.append((portal, kw))
            return {"success": True, "id": "987", "url": f"https://www.newgrounds.com/{'audio/listen' if portal == 'audio' else 'portal/view'}/987"}

        async def update_project(self, portal, pid, **kw):
            self.updated.append((portal, pid, kw))
            return {"success": True, "id": pid, "url": "u"}

    fake = FakeClient()
    monkeypatch.setattr(ngp, "NgClient", lambda username="", cookie="": fake)
    p = NewgroundsPoster()
    assert p.validate(_pkg(env["piece"])) == []
    res = asyncio.run(p.post(_pkg(env["piece"], rating="adult", extra={"ng_genre": "10"})))
    assert res.success and res.external_id == "audio:987" and "audio/listen" in res.external_url
    portal, kw = fake.submitted[0]
    assert portal == "audio" and kw["genre_id"] == "10" and kw["rating"] == "adult" and kw["tags"] == ["sample", "field recording"]
    assert kw["icon_path"] is None or Path(kw["icon_path"]).suffix == ".jpg"
    res = asyncio.run(p.post(_pkg(env["piece"], file_type="mp4", media_kind="video")))
    assert res.success and res.external_id == "movie:987" and fake.submitted[1][0] == "movie"
    res = asyncio.run(p.edit("movie:987", _pkg(env["piece"], title="Renamed")))
    assert res.success and fake.updated[0][:2] == ("movie", "987") and fake.updated[0][2]["title"] == "Renamed"
    assert NewgroundsPoster.split_external_id("42") == ("audio", "42")
    assert not asyncio.run(p.replace_file("audio:1", "x")).success
    # any rating is welcome; an image goes to the Art Portal (4.29.0); no session says so
    assert p.rating_refusal(_pkg(env["piece"], rating="adult")) is None
    assert p.media_refusal(_pkg(env["piece"], file_type="png", media_kind="image")) is None
    res = asyncio.run(p.post(_pkg(env["piece"], file_type="png", media_kind="image")))
    assert res.success and res.external_id == "art:987" and fake.submitted[-1][0] == "art"
    assert NewgroundsPoster.split_external_id("art:kk/piece") == ("art", "kk/piece")
    env["settings"].pop("ng_cookie")
    assert any("not connected" in e for e in p.validate(_pkg(env["piece"])))


# ── the registries ───────────────────────────────────────────────────────────

def test_newgrounds_is_registered_everywhere():
    from database import accounts as adb, platform_metrics, followers, analytics_queries
    from polling import session_check
    from polling.multi_account import get_poll_cycles
    from mirror import registry as mreg
    from routes import api as api_mod
    from auth.browser_login import PLATFORM_LOGIN
    assert "ng" in adb.PLATFORMS and adb.PLATFORM_NAMES["ng"] == "Newgrounds" and "ng" not in adb.POST_ONLY_PLATFORMS
    assert adb.DEFAULT_CRED_CHECKS["ng"]({"ng_cookie": "x"}) and not adb.DEFAULT_CRED_CHECKS["ng"]({})
    assert adb._HANDLE_KEYS["ng"] == ["ng_username"]
    assert "ng_cookie" in config.CREDENTIAL_FIELDS and "ng_username" not in config.CREDENTIAL_FIELDS
    assert config.PLATFORM_CREDENTIAL_FIELDS["ng"] == ["ng_username", "ng_cookie"]
    spec = platform_metrics.get("ng")
    assert spec.table == "ng_submissions" and "score" in spec.extra and spec.label_for("faves") == "Faves"
    assert "ng" in followers.FOLLOWER_PLATFORMS and analytics_queries.INSIGHT_TABLES["ng"] == "ng_submissions"
    assert "ng" in session_check.CHECKABLE and session_check.LABELS["ng"] == "Newgrounds"
    assert "ng" in get_poll_cycles() and "ng_" in mreg.PLATFORM_PREFIXES
    assert any(c[0] == "ng" for c in api_mod._PLATFORM_HEALTH_CONFIG)
    ng_login = PLATFORM_LOGIN["ng"]
    assert ng_login["fields"][0]["id"] == "ng_username" and "ng_cookie" in ng_login["extract"]({"a": "1"}, "")
    assert ng_login["success_check"]({"a": "1"}, "https://www.newgrounds.com/") and not ng_login["success_check"]({}, "https://www.newgrounds.com/passport")
    from posting.manager import _get_poster
    assert _get_poster("ng").platform_name == "Newgrounds"
    root = Path(__file__).resolve().parent.parent
    pj = (root / "frontend" / "js" / "platforms.js").read_text(encoding="utf-8")
    assert "code: 'ng'" in pj and "ng:   V({ faves: 'Faves' })," in pj
    app_js = (root / "frontend" / "js" / "app.js").read_text(encoding="utf-8")
    for needle in ("renderNGDashboard", "data-platform=\"ng\"", "ng-browser-login-btn", "ng-connect-btn", "tableFn: 'ngPollLogTable'"):
        assert needle in app_js, needle
    comp = (root / "frontend" / "js" / "components.js").read_text(encoding="utf-8")
    assert "ngPollLogTable(polls)" in comp and "ngTopList(" in comp
    api_js = (root / "frontend" / "js" / "api.js").read_text(encoding="utf-8")
    assert "ngConnect(data)" in api_js and "getNGAuthStatus" in api_js
