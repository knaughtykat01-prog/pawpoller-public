"""Telegram sends the video / audio itself — MEDIATYPES phase 2, site 1 (4.19.0).

Until 4.18.0 a video or audio piece could only be ANNOUNCED on Telegram with its poster. Now the
file goes to the channel: ``sendVideo`` (inline, streaming) or ``sendAudio`` (inline player with
title / performer), or ``sendDocument`` for the untouched bytes, always with the poster fitted to
Telegram's ``thumbnail`` rules (JPEG, ≤ 320 px, ≤ 200 kB). The Bot API's 50 MB multipart cap is
refused in validate(), before any bytes move.

The network is faked. No media file enters the repo: the "video" is a few bytes behind a .webm
name; the poster is a real PNG drawn by Pillow because the thumbnail fitter reads it.
"""
from __future__ import annotations

import asyncio
import io
import os

import httpx
import pytest

from clients.tg.client import TgClient
from posting.platforms.base import StoryUploadPackage
from posting.platforms.telegram import TelegramPoster, _performer


def _png(w, h) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (30, 120, 200)).save(buf, format="PNG")
    return buf.getvalue()


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _FakeAsyncClient:
    """Records each POST's method, form data and the names + sizes of the files sent."""
    calls: list = []

    def __init__(self, *a, **k):
        _FakeAsyncClient.kwargs = k

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, data=None, files=None):
        method = url.rsplit("/", 1)[-1]
        sent = {}
        for key, val in (files or {}).items():
            fh = val[1]
            blob = fh.read()
            sent[key] = {"name": val[0], "bytes": len(blob), "jpeg": blob[:3] == b"\xff\xd8\xff"}
        _FakeAsyncClient.calls.append({"method": method, "data": dict(data or {}), "files": sent})
        return _FakeResp({"ok": True, "result": {"message_id": 77}})


@pytest.fixture(autouse=True)
def _fake_net(monkeypatch):
    _FakeAsyncClient.calls = []
    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def media(tmp_path):
    clip = tmp_path / "clip.webm"
    clip.write_bytes(b"\x1a\x45\xdf\xa3" + b"\x00" * 500)
    track = tmp_path / "track.mp3"
    track.write_bytes(b"ID3" + b"\x00" * 300)
    poster = tmp_path / "poster.png"                       # a Library poster: big PNG, not a Telegram thumb
    poster.write_bytes(_png(1600, 900))
    return {"clip": str(clip), "track": str(track), "poster": str(poster)}


# ── the client ───────────────────────────────────────────────────────────────

def test_video_goes_by_send_video_with_measurements_and_a_fitted_thumbnail(media):
    c = TgClient("tok", "@chan")
    r = _run(c.create_media_post("cap", media["clip"], "video", poster=media["poster"],
                                 meta={"duration_s": 2.18, "width": 640, "height": 360}, spoiler=True))
    call = _FakeAsyncClient.calls[0]
    assert call["method"] == "sendVideo"
    assert call["data"]["caption"] == "cap" and call["data"]["has_spoiler"] == "true"
    assert call["data"]["duration"] == "2" and call["data"]["width"] == "640" and call["data"]["height"] == "360"
    assert call["data"]["supports_streaming"] == "true"
    assert call["files"]["video"]["name"] == "clip.webm" and call["files"]["video"]["bytes"] == 504
    th = call["files"]["thumbnail"]
    assert th["jpeg"] and th["bytes"] <= TgClient.THUMB_MAX_BYTES   # 1600×900 PNG → ≤320 px JPEG
    assert r == {"id": "77", "url": "https://t.me/chan/77"}
    # the temp thumbnail is cleaned up; the poster itself is untouched
    assert os.path.isfile(media["poster"]) and not [f for f in os.listdir(os.path.dirname(media["poster"])) if f.startswith("pp-tg-thumb")]
    assert _FakeAsyncClient.kwargs["timeout"] >= 600           # a 50 MB upload is minutes, not seconds


def test_audio_goes_by_send_audio_with_title_and_performer(media):
    c = TgClient("tok", "@chan")
    _run(c.create_media_post("cap", media["track"], "audio", poster=media["poster"],
                             meta={"duration_s": 190.4}, title="Sample Track", performer="Inkwolf",
                             silent=True, protect=True))
    call = _FakeAsyncClient.calls[0]
    assert call["method"] == "sendAudio"
    assert call["data"]["title"] == "Sample Track" and call["data"]["performer"] == "Inkwolf"
    assert call["data"]["duration"] == "190" and "width" not in call["data"]
    assert call["data"]["disable_notification"] == "true" and call["data"]["protect_content"] == "true"
    assert call["files"]["audio"]["name"] == "track.mp3" and call["files"]["thumbnail"]["jpeg"]


def test_as_document_keeps_the_bytes_and_still_carries_the_thumbnail(media):
    c = TgClient("tok", "@chan")
    _run(c.create_media_post("cap", media["clip"], "video", poster=media["poster"], as_document=True, pin=True))
    methods = [x["method"] for x in _FakeAsyncClient.calls]
    assert methods == ["sendDocument", "pinChatMessage"]
    assert _FakeAsyncClient.calls[0]["files"]["document"]["name"] == "clip.webm"
    assert _FakeAsyncClient.calls[0]["files"]["thumbnail"]["jpeg"]


def test_a_compliant_poster_is_sent_as_is_and_no_poster_is_fine(media, tmp_path):
    from PIL import Image
    small = tmp_path / "small.jpg"
    Image.new("RGB", (320, 180), (9, 9, 9)).save(small, format="JPEG", quality=80)
    c = TgClient("tok", "@chan")
    assert c._thumbnail_for(str(small)) == str(small)
    assert c._thumbnail_for(None) is None and c._thumbnail_for(str(tmp_path / "missing.png")) is None
    bad = tmp_path / "bad.png"
    bad.write_bytes(b"not an image")
    assert c._thumbnail_for(str(bad)) is None                # unreadable poster → post without one, never raise
    _run(c.create_media_post("cap", media["track"], "audio"))
    assert "thumbnail" not in _FakeAsyncClient.calls[0]["files"]


def test_only_video_or_audio_and_only_a_real_file(media):
    c = TgClient("tok", "@chan")
    with pytest.raises(ValueError):
        _run(c.create_media_post("cap", media["poster"], "image"))
    with pytest.raises(ValueError):
        _run(c.create_media_post("cap", media["clip"] + ".missing", "video"))
    assert _FakeAsyncClient.calls == []


# ── the poster ───────────────────────────────────────────────────────────────

def _pkg(**kw) -> StoryUploadPackage:
    base = dict(story_name="", chapter_index=0, chapter_title="", platform="tg",
                title="Sample Clip", description="A short loop", tags=["loop"], rating="general",
                file_path=None, file_type="")
    base.update(kw)
    return StoryUploadPackage(**base)


@pytest.fixture
def creds(monkeypatch):
    import config
    monkeypatch.setattr(config, "get_settings", lambda: {"tg_bot_token": "tok", "tg_channel": "@chan"})
    monkeypatch.setattr(TelegramPoster, "_resolve_creds", lambda self, *a, **k: {"tg_bot_token": "tok", "tg_channel": "@chan"})


class _FakeClient:
    media_calls: list = []
    post_calls: list = []

    def __init__(self, **kw):
        self.channel = "@chan"

    async def create_media_post(self, text, path, kind, **kw):
        _FakeClient.media_calls.append({"text": text, "path": path, "kind": kind, **kw})
        return {"id": "5", "url": "https://t.me/chan/5"}

    async def create_post(self, text, **kw):
        _FakeClient.post_calls.append({"text": text, **kw})
        return {"id": "6", "url": "https://t.me/chan/6"}


@pytest.fixture
def fake_client(monkeypatch):
    _FakeClient.media_calls, _FakeClient.post_calls = [], []
    monkeypatch.setattr("clients.tg.client.TgClient", _FakeClient)
    monkeypatch.setattr("database.tg_queries.record_submission", lambda *a, **k: None)
    return _FakeClient


def test_a_video_piece_is_sent_not_announced(media, creds, fake_client):
    p = TelegramPoster()
    pkg = _pkg(file_path=media["clip"], file_type="webm", thumbnail_path=media["poster"], media_kind="video",
               duration_s=2.18, width=640, height=360, extra={"artist_name": "Inkwolf", "document": True})
    r = _run(p.post(pkg))
    assert r.success and r.external_url == "https://t.me/chan/5"
    assert fake_client.post_calls == []
    call = fake_client.media_calls[0]
    assert call["kind"] == "video" and call["path"] == media["clip"] and call["poster"] == media["poster"]
    assert call["meta"] == {"duration_s": 2.18, "width": 640, "height": 360}
    assert call["as_document"] is True and call["title"] == "Sample Clip" and call["performer"] == "Inkwolf"
    assert "#loop" in call["text"] and "A short loop" in call["text"]


def test_an_image_piece_still_takes_the_photo_path(media, creds, fake_client, tmp_path):
    pic = tmp_path / "pic.png"
    pic.write_bytes(_png(8, 8))
    r = _run(TelegramPoster().post(_pkg(file_path=str(pic), file_type="png", media_kind="image")))
    assert r.success and fake_client.media_calls == [] and fake_client.post_calls[0]["image_paths"] == [str(pic)]


def test_validate_refuses_media_over_the_bot_api_cap(media, creds, monkeypatch):
    p = TelegramPoster()
    ok = p.validate(_pkg(file_path=media["clip"], file_type="webm", thumbnail_path=media["poster"], media_kind="video"))
    assert ok == []
    monkeypatch.setattr(TgClient, "MEDIA_UPLOAD_CAP", 100)
    errs = p.validate(_pkg(file_path=media["clip"], file_type="webm", thumbnail_path=media["poster"], media_kind="video"))
    assert len(errs) == 1 and "at most 50 MB" in errs[0] and errs[0].startswith("Video is")
    errs = p.validate(_pkg(file_path=media["clip"] + ".gone", file_type="webm", media_kind="video"))
    assert any("File not found" in e for e in errs)


def test_the_performer_is_the_credited_artist_or_nothing():
    assert _performer(_pkg(extra={"artist_name": "Inkwolf"})) == "Inkwolf"
    assert _performer(_pkg(extra={})) == "" and _performer(_pkg(extra={"artist_name": "  "})) == ""


def test_the_package_carries_the_artist_name_for_the_performer_line(tmp_path, monkeypatch):
    from posting import artwork_reader as ar
    monkeypatch.setattr(ar, "get_artwork_archive_path", lambda: tmp_path / "art")
    (tmp_path / "art").mkdir()
    name = ar.create_artwork(title="Sample Track", image_filename="track.mp3", image_bytes=b"ID3" + b"\x00" * 50,
                             artist={"name": "Inkwolf", "handles": {}},
                             thumbnail_filename="poster.png", thumbnail_bytes=_png(4, 4), media={"duration_s": 5})
    pkg = ar.build_artwork_package(ar.load_artwork(name), "tg")
    assert pkg.media_kind == "audio" and pkg.extra.get("artist_name") == "Inkwolf"
