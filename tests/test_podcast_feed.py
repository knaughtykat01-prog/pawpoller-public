"""The podcast feed platform — MEDIAPLATS §4 (4.21.1).

A feed PawPoller serves itself: rows in two local tables, an RSS document rendered from them,
public routes for the document / art / audio (Range) / episode page, and a `pod` poster that
turns "publish to this feed" into an episode row. No media file enters the repo: the audio is a
few bytes behind an .mp3 name; the poster is a Pillow PNG.
"""
from __future__ import annotations

import io
import json
import os
import re
import xml.etree.ElementTree as ET

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import config
from database import accounts as adb
from database import podcasts as pdb
from database.db import get_connection
from posting import podcast_feed
from posting import artwork_reader as ar


def _png(w=64, h=36) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (200, 90, 120)).save(buf, format="PNG")
    return buf.getvalue()


FAKE_MP3 = b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\xff\xfb" * 100


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(ar, "get_artwork_archive_path", lambda: tmp_path / "art")
    (tmp_path / "art").mkdir()
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    (tmp_path / "data").mkdir()
    settings = {"ig_public_base_url": "https://pp.example"}
    monkeypatch.setattr(config, "get_settings", lambda: dict(settings))
    saved = {}
    monkeypatch.setattr(config, "save_settings", lambda d: (settings.update(d), saved.update(d)))
    monkeypatch.setattr(config, "delete_settings_keys", lambda keys: [settings.pop(k, None) for k in keys])
    name = ar.create_artwork(title="Sample Track", image_filename="track.mp3", image_bytes=FAKE_MP3,
                             description="A tune.", rating="general", tags={"default": ["sample"]},
                             thumbnail_filename="poster.png", thumbnail_bytes=_png(), media={"duration_s": 190.4})
    return {"piece": name, "settings": settings, "saved": saved, "tmp": tmp_path}


@pytest.fixture
def api(env):
    from routes import podcast_api
    app = FastAPI()
    app.include_router(podcast_api.podcast_router)
    app.include_router(podcast_api.feed_router)
    return TestClient(app)


# ── the tables ───────────────────────────────────────────────────────────────

def test_feeds_and_episodes_round_trip_and_the_guid_never_changes(env):
    conn = get_connection()
    try:
        fid = pdb.create_feed(conn, slug="sample-show", title="Sample Show", author="Inkwolf", explicit=True)
        assert pdb.get_feed_by_slug(conn, "sample-show")["explicit"] is True
        ep = pdb.add_episode(conn, feed_id=fid, artwork_name=env["piece"], title="Ep 1", notes="n", explicit=False)
        again = pdb.add_episode(conn, feed_id=fid, artwork_name=env["piece"], title="Ep 1 renamed")
        assert again["guid"] == ep["guid"] and again["title"] == "Ep 1"        # re-adding returns the row
        pdb.update_episode(conn, ep["episode_id"], title="Ep 1 renamed", explicit=True)
        assert pdb.get_episode(conn, ep["episode_id"])["explicit"] is True
        assert [e["feed_slug"] for e in pdb.episodes_for_piece(conn, env["piece"])] == ["sample-show"]
        pdb.remove_episode(conn, ep["episode_id"])
        assert pdb.list_episodes(conn, fid) == []
        with pytest.raises(ValueError):
            pdb.create_feed(conn, slug="Bad Slug!", title="x")
        with pytest.raises(ValueError):
            pdb.create_feed(conn, slug="ok", title="  ")
        pdb.delete_feed(conn, fid)
        assert pdb.list_feeds(conn) == []
    finally:
        conn.close()
    assert pdb.slugify("Sample Show: Season 2!") == "sample-show-season-2"


# ── the renderer ─────────────────────────────────────────────────────────────

def test_the_feed_document_carries_what_the_directories_require():
    feed = {"feed_id": 1, "slug": "sample-show", "title": "Sample Show", "description": "About & more", "author": "Inkwolf",
            "owner_email": "owner@example.com", "language": "en", "category": "Arts", "explicit": True,
            "artwork_name": "Cover", "artwork_file": None, "link": "https://example.com/show"}
    eps = [{"guid": "abc123", "artwork_name": "Sample_Track", "title": "Ep <1>", "notes": "Notes", "explicit": False,
            "season": 1, "episode_number": 3, "published_at": "2026-09-08 07:00:00"},
           {"guid": "missing", "artwork_name": "Gone", "title": "Gone", "notes": "", "explicit": False,
            "season": None, "episode_number": None, "published_at": "2026-09-07 07:00:00"}]
    pieces = {"Sample_Track": {"file": "/x/track.mp3", "bytes": 1234, "duration_s": 190.4, "has_poster": True}}
    xml = podcast_feed.render_feed(feed, eps, pieces, "https://pp.example/")
    assert xml == podcast_feed.render_feed(feed, eps, pieces, "https://pp.example/")     # deterministic
    root = ET.fromstring(xml)
    ns = {"itunes": podcast_feed.ITUNES_NS, "atom": podcast_feed.ATOM_NS}
    ch = root.find("channel")
    assert ch.find("title").text == "Sample Show" and ch.find("language").text == "en"
    assert ch.find("atom:link", ns).attrib["href"] == "https://pp.example/feed/sample-show.xml"
    assert ch.find("itunes:explicit", ns).text == "true" and ch.find("itunes:category", ns).attrib["text"] == "Arts"
    assert ch.find("itunes:owner/itunes:email", ns).text == "owner@example.com"
    assert ch.find("itunes:image", ns).attrib["href"] == "https://pp.example/feed/sample-show/art.jpg"
    items = ch.findall("item")
    assert len(items) == 1, "an episode whose piece is missing is left out"
    it = items[0]
    assert it.find("title").text == "Ep <1>"
    g = it.find("guid")
    assert g.text == "abc123" and g.attrib["isPermaLink"] == "false"
    enc = it.find("enclosure")
    assert enc.attrib == {"url": "https://pp.example/feed/sample-show/abc123.mp3", "length": "1234", "type": "audio/mpeg"}
    assert it.find("itunes:duration", ns).text == "190" and it.find("itunes:explicit", ns).text == "false"
    assert it.find("itunes:season", ns).text == "1" and it.find("itunes:episode", ns).text == "3"
    assert it.find("itunes:image", ns).attrib["href"] == "https://pp.example/feed/sample-show/abc123/art.jpg"
    assert re.match(r"^[A-Z][a-z]{2}, \d\d [A-Z][a-z]{2} \d{4} \d\d:\d\d:\d\d \+0000$", it.find("pubDate").text)
    assert it.find("link").text == "https://pp.example/feed/sample-show/abc123"
    assert podcast_feed.crawler_name("Spotify/1.0") == "Spotify" and podcast_feed.crawler_name("iTMS") == "Apple Podcasts"
    assert podcast_feed.crawler_name("Mozilla/5.0") is None


# ── the routes ───────────────────────────────────────────────────────────────

def test_create_a_feed_makes_its_account_and_the_public_surface_serves_it(api, env):
    r = api.post("/api/podcasts", json={"title": "Sample Show", "author": "Inkwolf", "owner_email": "owner@example.com",
                                        "category": "Arts", "explicit": False, "description": "About"})
    assert r.status_code == 200, r.text
    fid, slug, acct = r.json()["feed_id"], r.json()["slug"], r.json()["account_id"]
    assert slug == "sample-show"
    conn = get_connection()
    try:
        a = adb.get_account(conn, acct)
        assert a["platform"] == "pod" and a["handle"] == "sample-show" and a["label"] == "Sample Show"
        key = config.account_setting_key(acct, "pod_feed_slug", bool(a["is_default"]))
    finally:
        conn.close()
    assert env["saved"][key] == "sample-show"
    assert api.post("/api/podcasts", json={"title": "Sample Show"}).status_code == 409
    assert api.post("/api/podcasts", json={"title": "x", "slug": "Bad!"}).status_code == 400
    lst = api.get("/api/podcasts").json()
    assert lst["feeds"][0]["feed_url"] == "https://pp.example/feed/sample-show.xml" and "owner_email" not in lst["feeds"][0]
    assert lst["feeds"][0]["has_owner_email"] is True and lst["public_base"] == "https://pp.example"

    # publish an episode, then read everything a directory or a listener would
    r = api.post(f"/api/podcasts/{fid}/episodes", json={"artwork_name": env["piece"], "explicit": True})
    assert r.status_code == 200, r.text
    guid = r.json()["episode"]["guid"]
    assert api.post(f"/api/podcasts/{fid}/episodes", json={"artwork_name": "Nope"}).status_code == 400
    d = api.get(f"/api/podcasts/{fid}").json()
    assert d["episodes"][0]["piece_ok"] and d["episodes"][0]["duration_s"] == 190.4
    assert d["episodes"][0]["page_url"] == f"https://pp.example/feed/sample-show/{guid}"

    x = api.get("/feed/sample-show.xml", headers={"User-Agent": "Spotify/1.0"})
    assert x.status_code == 200 and x.headers["content-type"].startswith("application/rss+xml")
    assert f"https://pp.example/feed/sample-show/{guid}.mp3" in x.text and 'length="' + str(len(FAKE_MP3)) + '"' in x.text
    etag = x.headers["etag"]
    assert api.get("/feed/sample-show.xml", headers={"If-None-Match": etag}).status_code == 304
    assert api.get("/api/podcasts").json()["fetches"]["sample-show"]["Spotify"]["count"] == 1
    assert api.get("/feed/nope.xml").status_code == 404 and api.get("/feed/sample-show.json").status_code == 404

    a = api.get(f"/feed/sample-show/{guid}.mp3", headers={"Range": "bytes=0-9"})
    assert a.status_code == 206 and len(a.content) == 10 and a.headers["content-type"].startswith("audio/mpeg")
    assert api.get(f"/feed/sample-show/{guid}.wav").status_code == 404                # the piece's own type only
    page = api.get(f"/feed/sample-show/{guid}")
    assert page.status_code == 200 and "<audio" in page.text and "Sample Track" in page.text
    art = api.get(f"/feed/sample-show/{guid}/art.jpg")
    assert art.status_code == 200 and art.content[:3] == b"\xff\xd8\xff"
    from PIL import Image
    assert Image.open(io.BytesIO(art.content)).size == (1400, 1400)                # fitted square episode art
    # no feed art of its own yet: the newest episode's poster stands in (Apple refuses a channel without one)
    fb = api.get("/feed/sample-show/art.jpg")
    assert fb.status_code == 200 and Image.open(io.BytesIO(fb.content)).size == (1400, 1400)
    assert "feed/sample-show/art.jpg" in x.text and api.get(f"/api/podcasts/{fid}").json()["feed"]["art_from_episode"] is True

    # feed art upload, then the channel image is the feed's own
    r = api.post(f"/api/podcasts/{fid}/artwork", files={"file": ("cover.png", _png(300, 200), "image/png")})
    assert r.status_code == 200 and api.get("/feed/sample-show/art.jpg").status_code == 200
    assert "feed/sample-show/art.jpg" in api.get("/feed/sample-show.xml").text
    assert api.get(f"/api/podcasts/{fid}").json()["feed"]["art_from_episode"] is False
    assert api.post(f"/api/podcasts/{fid}/artwork", files={"file": ("x.mp3", b"ID3", "audio/mpeg")}).status_code == 415

    # unpublish, then delete the feed (the piece is untouched)
    eid = d["episodes"][0]["episode_id"]
    assert api.delete(f"/api/podcasts/episodes/{eid}").status_code == 200
    assert "<item>" not in api.get("/feed/sample-show.xml").text
    assert api.delete(f"/api/podcasts/{fid}").status_code == 200
    assert api.get("/feed/sample-show.xml").status_code == 404
    assert ar.load_artwork(env["piece"]).title == "Sample Track"
    assert key not in env["settings"]                                              # the slug setting went with the account
    conn = get_connection()
    try:
        assert adb.list_accounts(conn, "pod") == []
        # a stale slug must never seed a "Podcast feed (default)" account — the feed makes its own
        assert adb.seed_default_accounts(conn, {"pod_feed_slug": "ghost"}) == 0
        assert adb.list_accounts(conn, "pod") == []
    finally:
        conn.close()


def test_the_public_paths_never_reach_past_the_feed(api, env):
    assert api.get("/feed/../etc/passwd.xml").status_code in (404, 400)
    assert api.get("/feed/sample-show/../../x").status_code in (404, 400)


# ── the poster ───────────────────────────────────────────────────────────────

def _pkg(piece, **over):
    from posting.platforms.base import StoryUploadPackage
    kw = dict(story_name=piece, chapter_index=0, chapter_title="", platform="pod", title="Sample Track",
              description="A tune.", tags=["sample"], rating="adult", file_path="/tmp/track.mp3", file_type="mp3",
              media_kind="audio")
    kw.update(over)
    return StoryUploadPackage(**kw)


def test_the_poster_publishes_edits_and_refuses_honestly(api, env, monkeypatch):
    import asyncio
    from posting.platforms.podcast import PodcastPoster
    r = api.post("/api/podcasts", json={"title": "Sample Show"})
    fid, acct = r.json()["feed_id"], r.json()["account_id"]
    p = PodcastPoster()
    p.account_id = acct
    real = ar.load_artwork(env["piece"]).image_path
    assert p.validate(_pkg(env["piece"], file_path=real)) == []
    res = asyncio.run(p.post(_pkg(env["piece"])))
    assert res.success and res.external_url == f"https://pp.example/feed/sample-show/{res.external_id}"
    conn = get_connection()
    try:
        ep = pdb.get_episode_by_guid(conn, fid, res.external_id)
        assert ep["title"] == "Sample Track" and ep["notes"] == "A tune." and ep["explicit"] is True   # adult → explicit
    finally:
        conn.close()
    res2 = asyncio.run(p.edit(res.external_id, _pkg(env["piece"], title="Renamed", rating="general")))
    assert res2.success
    conn = get_connection()
    try:
        ep = pdb.get_episode_by_guid(conn, fid, res.external_id)
        assert ep["title"] == "Renamed" and ep["explicit"] is False
    finally:
        conn.close()
    # the gates: a video / an image piece is refused with the reason; no feed → says so
    from posting.manager import _get_poster
    assert _get_poster("pod").media_refusal(_pkg(env["piece"], file_type="mp4", media_kind="video")).startswith("Podcast feed doesn't take mp4 video")
    assert _get_poster("pod").media_refusal(_pkg(env["piece"], file_type="png", media_kind="image")).startswith("Podcast feed doesn't take png image")
    env["settings"]["ig_public_base_url"] = ""
    errs = p.validate(_pkg(env["piece"], file_path=real))
    assert any("public address" in e for e in errs)
    q = PodcastPoster()
    q.account_id = acct
    env["settings"][config.account_setting_key(acct, "pod_feed_slug", True)] = "vanished"
    env["settings"]["pod_feed_slug"] = "vanished"
    assert any("no longer exists" in e for e in q.validate(_pkg(env["piece"], file_path=real)))
    assert q.requires_mode == "server" and q.supports_edit and q.max_rating == "adult"


def test_the_platform_is_registered_everywhere_the_pickers_and_tests_look():
    from posting.manager import _get_poster
    assert type(_get_poster("pod")).__name__ == "PodcastPoster"
    assert "pod" in adb.PLATFORMS and "pod" in adb.POST_ONLY_PLATFORMS and adb.PLATFORM_NAMES["pod"] == "Podcast feed"
    assert adb.DEFAULT_CRED_CHECKS["pod"]({"pod_feed_slug": "x"}) and not adb.DEFAULT_CRED_CHECKS["pod"]({})
    assert adb._HANDLE_KEYS["pod"] == ["pod_feed_slug"] and config.PLATFORM_CREDENTIAL_FIELDS["pod"] == ["pod_feed_slug"]
    js = open("frontend/js/platforms.js", encoding="utf-8").read()
    assert "code: 'pod'" in js
    art = open("frontend/js/artwork.js", encoding="utf-8").read()
    line = next(l for l in art.splitlines() if "_PLATFORMS:" in l)
    assert "'pod'" in line
    dash = open("dashboard.py", encoding="utf-8").read()
    assert '"/feed/"' in dash.split("_AUTH_EXEMPT_PREFIXES = ")[1].split("\n")[0]
    assert "media-src 'self'" in dash
