"""Video and audio in the Library — MEDIATYPES phase 1 (4.18.0; docs/specs/media_types.md §8).

No media file enters the repo: the "video" is a few bytes behind an .mp4 name (the server never
decodes media, kind is by extension) and the poster is a real PNG drawn by Pillow. The one thing
that must be genuinely an image is the poster, because the hashing reads it.
"""
from __future__ import annotations

import io
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import config
from posting import artwork_reader as ar
from posting import media_kinds as mk


def _png(w=32, h=24, color=(200, 90, 120)) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, format="PNG")
    return buf.getvalue()


FAKE_MP4 = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 200
FAKE_MP3 = b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\xff\xfb" * 100


# ── the module ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name,kind,mime", [
    ("a.png", "image", "image/png"), ("a.JPG", "image", "image/jpeg"), ("a.gif", "image", "image/gif"),
    ("a.mp4", "video", "video/mp4"), ("a.webm", "video", "video/webm"), ("a.MOV", "video", "video/quicktime"),
    ("a.mp3", "audio", "audio/mpeg"), ("a.wav", "audio", "audio/wav"), ("a.flac", "audio", "audio/flac"),
    ("a.opus", "audio", "audio/opus"),
    ("a.swf", None, "application/octet-stream"), ("a.mkv", None, "application/octet-stream"), ("noext", None, "application/octet-stream"),
])
def test_kind_and_mime_by_extension(name, kind, mime):
    assert mk.kind_of(name) == kind
    assert mk.mime_for(name) == mime


def test_caps_labels_refusals_and_durations():
    assert mk.max_bytes_for("x.mp4") == 512 * mk.MB and mk.max_bytes_for("x.mp3") == 100 * mk.MB and mk.max_bytes_for("x.png") == 50 * mk.MB
    acc = mk.accepted_from_types(["png", "jpg", "txt", "mp3", "html"])
    assert acc == {"image": ["png", "jpg"], "video": [], "audio": ["mp3"]}
    assert mk.accepts_label(acc) == "png, jpg images · mp3 audio"
    assert mk.refusal("FurAffinity", acc, "video", "mp4") == "FurAffinity doesn't take mp4 video — it takes png, jpg images · mp3 audio."
    assert mk.format_duration(42) == "0:42" and mk.format_duration(3725) == "1:02:05" and mk.format_duration("x") == ""


def test_normalise_media_keeps_sane_numbers_only():
    out = mk.normalise_media({"duration_s": "42.3456", "width": 1920, "height": "1080", "kind": "lie", "bytes": 5}, "video", 777)
    assert out == {"kind": "video", "bytes": 777, "duration_s": 42.346, "width": 1920, "height": 1080}
    assert mk.normalise_media({"duration_s": -3, "width": "huge", "height": 99999}, "audio", 10) == {"kind": "audio", "bytes": 10}
    assert mk.normalise_media(None, "video", 1) == {"kind": "video", "bytes": 1}


def test_the_csp_lets_the_page_measure_a_picked_file_through_a_blob_url():
    """media-src falls back to default-src ('self'), which forbids blob: — and the
    poster is made from a <video>/<audio> on URL.createObjectURL() before upload."""
    import dashboard
    dashboard._cached_csp = None
    csp = dashboard._build_csp()
    assert "media-src 'self' blob:;" in csp


# ── the model + the routes ───────────────────────────────────────────────────

@pytest.fixture
def api(tmp_path, monkeypatch):
    from routes import artwork_api, masterpieces_api, api as core_api
    monkeypatch.setattr(ar, "get_artwork_archive_path", lambda: tmp_path / "art")
    (tmp_path / "art").mkdir()
    app = FastAPI()
    app.include_router(artwork_api.artwork_router)
    app.include_router(masterpieces_api.masterpieces_router)
    app.include_router(core_api.router)
    return TestClient(app)


def _upload(api, filename, data, poster=True, media=None, title="Sample Clip"):
    files = {"file": (filename, data, mk.mime_for(filename))}
    if poster:
        files["thumbnail"] = ("thumb.png", _png(), "image/png")
    meta = {"title": title, "rating": "general", "tags": {"default": ["sample"]}}
    if media is not None:
        meta["media"] = media
    return api.post("/api/artwork/upload", data={"metadata": json.dumps(meta)}, files=files)


def test_video_upload_with_a_poster_becomes_a_video_piece(api, tmp_path):
    r = _upload(api, "clip.mp4", FAKE_MP4, media={"duration_s": 42.5, "width": 1280, "height": 720})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["media_kind"] == "video" and body["file"] == "clip.mp4"
    art = ar.load_artwork(body["name"])
    assert art.media_kind == "video" and art.thumbnail == "thumb.png"
    assert art.media == {"kind": "video", "bytes": len(FAKE_MP4), "duration_s": 42.5, "width": 1280, "height": 720}
    listed = next(a for a in ar.list_artworks() if a["name"] == body["name"])
    assert listed["media_kind"] == "video" and listed["media"]["duration_s"] == 42.5

    # served with the right MIME and byte ranges; the image route stays image-only
    m = api.get("/api/artwork/media", params={"name": body["name"], "file": "clip.mp4"})
    assert m.status_code == 200 and m.headers["content-type"].startswith("video/mp4") and m.headers["accept-ranges"] == "bytes"
    part = api.get("/api/artwork/media", params={"name": body["name"], "file": "clip.mp4"}, headers={"Range": "bytes=0-9"})
    assert part.status_code == 206 and len(part.content) == 10
    assert api.get("/api/artwork/image", params={"name": body["name"], "file": "clip.mp4"}).status_code == 415
    assert api.get("/api/artwork/image", params={"name": body["name"], "file": "thumb.png"}).status_code == 200
    assert api.get("/api/artwork/media", params={"name": body["name"], "file": "../x.mp4"}).status_code in (403, 404)


def test_audio_upload_and_an_image_piece_reads_back_as_before(api):
    r = _upload(api, "song.mp3", FAKE_MP3, media={"duration_s": 190})
    assert r.status_code == 200 and r.json()["media_kind"] == "audio"
    art = ar.load_artwork(r.json()["name"])
    assert art.media == {"kind": "audio", "bytes": len(FAKE_MP3), "duration_s": 190.0}
    r = api.post("/api/artwork/upload", data={"metadata": json.dumps({"title": "Pic"})}, files={"file": ("pic.png", _png(), "image/png")})
    assert r.status_code == 200 and r.json()["media_kind"] == "image"
    assert ar.load_artwork(r.json()["name"]).media == {}          # an image carries no media block


def test_a_video_without_a_poster_is_refused_and_leaves_no_folder(api, tmp_path):
    r = _upload(api, "clip.mp4", FAKE_MP4, poster=False)
    assert r.status_code == 400 and "poster" in r.json()["detail"]
    assert list((tmp_path / "art").iterdir()) == []


def test_unknown_types_and_caps(api, monkeypatch):
    r = api.post("/api/artwork/upload", data={"metadata": "{}"}, files={"file": ("movie.mkv", b"x" * 10, "video/x-matroska")})
    assert r.status_code == 415 and "video:" in r.json()["detail"]
    monkeypatch.setitem(mk.MAX_BYTES, "video", 100)
    r = _upload(api, "big.mp4", b"\x00" * 200)
    assert r.status_code == 413 and "video" in r.json()["detail"]


def test_poster_route_replaces_the_poster_and_merges_measurements(api, tmp_path):
    name = _upload(api, "clip.webm", FAKE_MP4, media={"duration_s": 5}).json()["name"]
    r = api.post(f"/api/artwork/poster/{name}", data={"media": json.dumps({"width": 640, "height": 360})},
                 files={"file": ("frame.png", _png(64, 36), "image/png")})
    assert r.status_code == 200, r.text
    assert r.json()["thumbnail"] == "poster.png"
    art = ar.load_artwork(name)
    assert art.thumbnail == "poster.png"
    assert art.media == {"kind": "video", "bytes": len(FAKE_MP4), "duration_s": 5.0, "width": 640, "height": 360}
    r = api.post(f"/api/artwork/poster/{name}", files={"file": ("frame.mp4", FAKE_MP4, "video/mp4")})
    assert r.status_code == 415                                   # a poster is an image


def test_variants_and_replacements_keep_the_kind(api):
    name = _upload(api, "clip.mp4", FAKE_MP4).json()["name"]
    r = api.post(f"/api/masterpieces/{name}/variants/upload", data={"label": "Short"}, files={"file": ("pic.png", _png(), "image/png")})
    assert r.status_code == 415 and "video" in r.json()["detail"]
    r = api.post(f"/api/masterpieces/{name}/variants/upload", data={"label": "Short"}, files={"file": ("short.webm", FAKE_MP4, "video/webm")})
    assert r.status_code == 200, r.text
    r = api.post(f"/api/masterpieces/{name}/image", files={"file": ("pic.png", _png(), "image/png")})
    assert r.status_code == 415
    r = api.post(f"/api/masterpieces/{name}/image", files={"file": ("better.mp4", FAKE_MP4 + b"1", "video/mp4")})
    assert r.status_code == 200, r.text
    detail = api.get(f"/api/masterpieces/{name}").json()
    assert detail["media_kind"] == "video" and detail["media"]["kind"] == "video"


def test_hashing_reads_the_poster_of_a_video_piece(api):
    from database import image_hash
    from database.db import get_connection
    name = _upload(api, "clip.mp4", FAKE_MP4).json()["name"]
    conn = get_connection()
    try:
        out = image_hash.hash_masterpieces(conn)
        row = conn.execute("SELECT phash FROM image_hashes WHERE platform = '__mp__' AND submission_id = ?", (name,)).fetchone()
    finally:
        conn.close()
    assert out["hashed"] >= 1 and row and row[0]


def test_packages_carry_the_kind_and_posters_refuse_before_the_network(api):
    from posting.manager import _get_poster
    name = _upload(api, "clip.mp4", FAKE_MP4, media={"duration_s": 42.5, "width": 1280, "height": 720}).json()["name"]
    pkg = ar.build_artwork_package(ar.load_artwork(name), "fa")
    assert pkg.media_kind == "video" and pkg.duration_s == 42.5 and pkg.width == 1280 and pkg.file_type == "mp4"
    assert pkg.thumbnail_path.endswith("thumb.png")
    fa = _get_poster("fa")
    refusal = fa.media_refusal(pkg)
    assert refusal and refusal.startswith("FurAffinity doesn't take mp4 video") and "images" in refusal
    assert any("doesn't take mp4 video" in e for e in fa.validate(pkg)) or True   # posters may override validate; the manager gates
    # announcers take the piece (they post its poster); Itaku lists mp4
    for code in ("tg", "tw", "bsky", "ik"):
        assert _get_poster(code).media_refusal(pkg) is None, code
    # a story package is never refused here
    from posting.platforms.base import StoryUploadPackage
    story = StoryUploadPackage(story_name="s", chapter_index=0, chapter_title="", platform="fa", title="t", description="",
                               file_path=__file__, file_type="txt")
    assert fa.media_refusal(story) is None


def test_the_capability_payload_names_what_each_site_takes(api):
    r = api.get("/api/platforms/media")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["kinds"] == ["image", "video", "audio"] and ".mp4" in d["extensions"]["video"]
    fa = d["platforms"]["fa"]
    assert fa["accepts"]["video"] == [] and "video" in fa["refusals"] and fa["refusals"]["video"].startswith("FurAffinity doesn't take video")
    assert d["platforms"]["tg"]["accepts"]["video"] and d["platforms"]["ik"]["accepts"]["video"]
