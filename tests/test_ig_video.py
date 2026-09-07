"""Instagram Reels through the hosting ladder — MEDIATYPES phase 3, site 3 (4.20.1).

Meta cURLs a Reel's `video_url` (and `cover_url`) exactly as it cURLs an image's `image_url`, so
the same stash / relay / tunnel ladder carries the video — byte-for-byte, never re-encoded — and
the poster goes along as the cover. The network is faked; the "video" is a minimal ISO-BMFF
header behind an .mp4 name (the `ftyp` box is what the stash and the relay look for).
"""
from __future__ import annotations

import asyncio

import pytest

import config
from posting import ig_media
from posting.platforms.base import StoryUploadPackage
from posting.platforms.instagram import InstagramPoster, _is_video

MP4 = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2" + b"\x00" * 64
MOV = b"\x00\x00\x00\x14ftypqt  \x00\x00\x00\x00qt  " + b"\x00" * 64


@pytest.fixture
def stash_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    return tmp_path / "ig_pending"


def _png(w=4, h=4) -> bytes:
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (1, 2, 3)).save(buf, format="PNG")
    return buf.getvalue()


# ── the stash ────────────────────────────────────────────────────────────────

def test_a_video_is_stashed_as_it_is_and_served_by_its_own_type(stash_dir, tmp_path):
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(MP4)
    tok = ig_media.stash_file(str(clip))
    p = ig_media.path_for(tok)
    assert p and p.suffix == ".mp4" and p.read_bytes() == MP4                 # never re-encoded
    assert ig_media.path_for(tok + ".mp4") == p and ig_media.path_for(tok + ".jpg") == p
    assert ig_media.mime_for(p) == "video/mp4" and ig_media.ext_for(tok) == ".mp4"
    assert ig_media.public_url("https://pp.example/", tok) == f"https://pp.example/api/ig/pubmedia/{tok}.mp4"
    assert ig_media.pending_count() == 1
    ig_media.cleanup(tok)
    assert ig_media.path_for(tok) is None and ig_media.pending_count() == 0
    with pytest.raises(ValueError):
        ig_media.stash_file(str(tmp_path / "x.webm"))


def test_stash_bytes_keeps_a_video_and_still_normalises_an_image(stash_dir):
    assert ig_media.is_video_bytes(MP4) and ig_media.is_video_bytes(MOV) and not ig_media.is_video_bytes(_png())
    tok = ig_media.stash_bytes(MOV)
    assert ig_media.path_for(tok).suffix == ".mov" and ig_media.mime_for(ig_media.path_for(tok)) == "video/quicktime"
    tok2 = ig_media.stash_bytes(_png())
    assert ig_media.path_for(tok2).suffix == ".jpg" and ig_media.path_for(tok2).read_bytes()[:3] == b"\xff\xd8\xff"


def test_path_for_still_refuses_anything_but_a_token(stash_dir):
    assert ig_media.path_for("../../etc/passwd") is None and ig_media.path_for("a" * 31 + ".mp4") is None


# ── the ladder ───────────────────────────────────────────────────────────────

def test_the_local_rung_hosts_a_video_and_its_cover(stash_dir, tmp_path):
    from posting import ig_host
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(MP4)
    cover = tmp_path / "cover.png"
    cover.write_bytes(_png())
    hosted = asyncio.run(ig_host.host_images([str(clip), str(cover)], {"ig_public_base_url": "https://me.example"}))
    assert hosted.how == "local" and hosted.urls[0].endswith(".mp4") and hosted.urls[1].endswith(".jpg")
    assert ig_media.pending_count() == 2
    asyncio.run(hosted.close())
    assert ig_media.pending_count() == 0


# ── the routes ───────────────────────────────────────────────────────────────

@pytest.fixture
def api(stash_dir, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routes import ig_api
    monkeypatch.setattr(config, "get_settings", lambda: {"ig_public_base_url": "https://pp.example", "ig_relay_open": True})
    monkeypatch.setattr(ig_api, "_relay_rate_ok", lambda ip, now=None: True)
    app = FastAPI()
    app.include_router(ig_api.ig_router)
    return TestClient(app)


def test_the_relay_and_the_paired_route_take_a_video_under_the_video_cap(api, monkeypatch):
    r = api.post("/api/ig/relay", files={"file": ("clip.mp4", MP4, "application/octet-stream")})
    assert r.status_code == 200, r.text
    url = r.json()["url"]
    assert url.endswith(".mp4")
    g = api.get(url.replace("https://pp.example", ""))
    assert g.status_code == 200 and g.headers["content-type"].startswith("video/mp4") and g.content == MP4
    r = api.post("/api/ig/pubmedia", files={"file": ("clip.mov", MOV, "application/octet-stream")})
    assert r.status_code == 200 and r.json()["url"].endswith(".mov")
    # an image still has the pixels rule and the 12 MB rule
    r = api.post("/api/ig/relay", files={"file": ("x.bin", b"not a file at all", "application/octet-stream")})
    assert r.status_code == 400 and "mp4" in r.json()["detail"]
    monkeypatch.setattr(config, "IG_RELAY_MAX_BYTES", 10)
    r = api.post("/api/ig/relay", files={"file": ("pic.png", _png(), "image/png")})
    assert r.status_code == 413
    # a video over the video cap is refused by the reader
    monkeypatch.setattr(config, "IG_RELAY_MAX_VIDEO_BYTES", 32)
    r = api.post("/api/ig/relay", files={"file": ("clip.mp4", MP4, "application/octet-stream")})
    assert r.status_code == 413


# ── the client + the poster ──────────────────────────────────────────────────

def test_the_client_creates_a_reels_container_with_the_cover_and_waits_longer(monkeypatch):
    from clients.ig.client import IgClient
    c = IgClient.__new__(IgClient)
    c.user_id = "42"
    calls = []

    async def _ok():
        return True

    async def _post(url, data):
        calls.append(("POST", url, dict(data)))
        return {"id": "C1" if url.endswith("/media") else "M1"}

    async def _get(url, params=None):
        calls.append(("GET", url, dict(params or {})))
        return {"status_code": "FINISHED", "permalink": "https://www.instagram.com/reel/abc/"}
    c.ensure_logged_in = _ok
    c._post_json = _post
    c._get_json = _get
    r = asyncio.run(c.create_video_post("cap #loop", "https://pp.example/api/ig/pubmedia/t.mp4",
                                        cover_url="https://pp.example/api/ig/pubmedia/c.jpg"))
    assert r == {"id": "M1", "url": "https://www.instagram.com/reel/abc/"}
    create = calls[0]
    assert create[1].endswith("/42/media")
    assert create[2] == {"caption": "cap #loop", "media_type": "REELS", "video_url": "https://pp.example/api/ig/pubmedia/t.mp4",
                         "cover_url": "https://pp.example/api/ig/pubmedia/c.jpg", "share_to_feed": "true"}
    assert IgClient.REEL_READY_TRIES >= 60


def _pkg(path, ft, kind, **over):
    kw = dict(story_name="Sample_Clip", chapter_index=0, chapter_title="", platform="ig",
              title="Sample Clip", description="a loop", tags=["loop"], rating="general",
              file_path=str(path), file_type=ft, media_kind=kind, thumbnail_path="")
    kw.update(over)
    return StoryUploadPackage(**kw)


def test_the_poster_hosts_the_video_and_cover_and_publishes_a_reel(stash_dir, tmp_path, monkeypatch):
    config.save_settings({"ig_public_base_url": "https://pp.example", "ig_access_token": "TOKEN", "ig_user_id": "42"})
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(MP4)
    cover = tmp_path / "cover.png"
    cover.write_bytes(_png())
    captured = {}

    class FakeIg:
        def __init__(self, access_token="", user_id=""):
            pass

        async def create_video_post(self, caption, video_url, cover_url=None):
            captured.update(caption=caption, video_url=video_url, cover_url=cover_url)
            return {"id": "M1", "url": "https://www.instagram.com/reel/abc/"}

        async def create_post(self, *a, **k):
            raise AssertionError("a video must not take the photo path")

        async def close(self):
            pass
    monkeypatch.setattr("clients.ig.client.IgClient", FakeIg)
    r = asyncio.run(InstagramPoster().post(_pkg(clip, "mp4", "video", thumbnail_path=str(cover))))
    assert r.success and r.external_id == "M1"
    assert captured["video_url"].endswith(".mp4") and captured["cover_url"].endswith(".jpg") and "#loop" in captured["caption"]
    assert ig_media.pending_count() == 0                                   # released after the publish


def test_is_video_and_the_reel_caps(tmp_path, monkeypatch):
    assert _is_video(_pkg("c.mp4", "mp4", "video")) and not _is_video(_pkg("c.webm", "webm", "video"))
    monkeypatch.setattr(config, "get_settings", lambda: {"ig_public_base_url": "https://pp.example"})
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(MP4)
    p = InstagramPoster()
    assert p.validate(_pkg(clip, "mp4", "video", duration_s=30.0)) == []
    errs = p.validate(_pkg(clip, "mp4", "video", duration_s=1000.0))
    assert len(errs) == 1 and "up to 15 minutes" in errs[0]


def test_webm_and_audio_refused_before_the_network(tmp_path):
    from posting.manager import _get_poster
    p = _get_poster("ig")
    for name, ft, kind in (("c.webm", "webm", "video"), ("t.mp3", "mp3", "audio")):
        f = tmp_path / name
        f.write_bytes(b"\x00")
        assert p.media_refusal(_pkg(f, ft, kind)).startswith(f"Instagram doesn't take {ft} {kind}")
    g = tmp_path / "c.mp4"
    g.write_bytes(b"\x00")
    assert p.media_refusal(_pkg(g, "mp4", "video")) is None
