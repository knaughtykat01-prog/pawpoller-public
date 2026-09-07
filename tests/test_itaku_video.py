"""Itaku video — MEDIATYPES phase 2, site 2 (4.19.1).

Itaku keeps videos in their own DRF collection: ``POST /api/galleries/videos/`` with the file in a
``video`` field, the same metadata and token as an image; the PATCH sibling edits it. PostyBirb posts
exactly this. The network is faked; the "video" is a few bytes behind an .mp4 name.
"""
from __future__ import annotations

import asyncio

import pytest

from clients.ik.client import IKClient
from posting.platforms.base import StoryUploadPackage
from posting.platforms.itaku import ItakuPoster, _gallery_kind


class _FakeResponse:
    def __init__(self, status_code=201):
        self.status_code = status_code
        self.text = ""

    def json(self):
        return {"id": 9001}


class _FakeHTTP:
    def __init__(self):
        self.calls: list[dict] = []

    async def post(self, url, data=None, files=None, headers=None, timeout=None):
        self.calls.append({"method": "POST", "url": url, "data": dict(data or {}),
                           "files": {k: v[0] for k, v in (files or {}).items()}, "timeout": timeout})
        return _FakeResponse()

    async def patch(self, url, data=None, headers=None, timeout=None):
        self.calls.append({"method": "PATCH", "url": url, "data": dict(data or {})})
        return _FakeResponse(200)


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def client():
    c = IKClient("someuser")
    c._http = _FakeHTTP()
    return c


def _pkg(tmp_path, **over) -> StoryUploadPackage:
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"\x00\x00\x00\x18ftypisom" + b"\x00" * 100)
    kw = dict(story_name="Sample_Clip", chapter_index=0, chapter_title="", platform="ik",
              title="Sample Clip", description="a loop", tags=["a", "b", "c", "d", "e"], rating="general",
              file_path=str(clip), file_type="mp4", media_kind="video")
    kw.update(over)
    return StoryUploadPackage(**kw)


def _poster(client, monkeypatch) -> ItakuPoster:
    p = ItakuPoster()

    async def _ensure():
        return client, "tok"
    monkeypatch.setattr(p, "_ensure_client", _ensure)
    return p


def test_upload_video_hits_the_videos_collection_with_a_video_field(client, tmp_path):
    clip = tmp_path / "clip.webm"
    clip.write_bytes(b"\x1a\x45\xdf\xa3" + b"\x00" * 50)
    r = _run(client.upload_video(str(clip), title="Sample Clip", tags=["a", "b", "c", "d", "e"],
                                 maturity_rating="NSFW", token="tok"))
    call = client._http.calls[0]
    assert call["url"].endswith("/api/galleries/videos/")
    assert call["files"] == {"video": "clip.webm"}
    assert call["data"]["title"] == "Sample Clip" and call["data"]["maturity_rating"] == "NSFW"
    assert call["timeout"] >= 600                                   # a 500 MB upload is minutes
    assert r == {"id": "9001", "url": "https://itaku.ee/video/9001"}


def test_upload_image_is_unchanged(client, tmp_path):
    pic = tmp_path / "pic.png"
    pic.write_bytes(b"\x89PNG")
    r = _run(client.upload_image(str(pic), title="Pic", token="tok"))
    call = client._http.calls[0]
    assert call["url"].endswith("/api/galleries/images/") and call["files"] == {"image": "pic.png"}
    assert r["url"] == "https://itaku.ee/image/9001" and call["timeout"] == 60.0


def test_edit_a_video_patches_the_videos_collection(client):
    r = _run(client.edit_image(9001, title="New", token="tok", kind="video"))
    call = client._http.calls[0]
    assert call["method"] == "PATCH" and call["url"].endswith("/api/galleries/videos/9001/")
    assert r["url"] == "https://itaku.ee/video/9001"
    _run(client.edit_image(9001, title="New", token="tok"))
    assert client._http.calls[1]["url"].endswith("/api/galleries/images/9001/")
    with pytest.raises(RuntimeError):
        _run(client.edit_image(9001, title="x", token="tok", kind="audio"))


def test_the_poster_routes_a_video_package_to_upload_video(client, tmp_path, monkeypatch):
    p = _poster(client, monkeypatch)
    r = _run(p.post(_pkg(tmp_path)))
    assert r.success and r.external_id == "9001" and r.external_url == "https://itaku.ee/video/9001"
    assert client._http.calls[0]["url"].endswith("/api/galleries/videos/")
    # an image still goes to images; a text package still makes a post (no gallery call)
    pic = tmp_path / "pic.png"
    pic.write_bytes(b"\x89PNG")
    _run(p.post(_pkg(tmp_path, file_path=str(pic), file_type="png", media_kind="image")))
    assert client._http.calls[1]["url"].endswith("/api/galleries/images/")


def test_the_poster_edits_a_video_through_the_videos_collection(client, tmp_path, monkeypatch):
    p = _poster(client, monkeypatch)
    r = _run(p.edit("9001", _pkg(tmp_path, title="Renamed")))
    assert r.success and client._http.calls[0]["url"].endswith("/api/galleries/videos/9001/")
    assert client._http.calls[0]["data"]["title"] == "Renamed"
    assert "share_on_feed" not in client._http.calls[0]["data"]


def test_gallery_kind_and_the_video_cap(tmp_path, monkeypatch):
    assert _gallery_kind(_pkg(tmp_path)) == "video"
    assert _gallery_kind(_pkg(tmp_path, media_kind="", file_type="mov")) == "video"      # pre-4.18.0 package
    assert _gallery_kind(_pkg(tmp_path, file_type="png", media_kind="image")) == "image"
    assert _gallery_kind(_pkg(tmp_path, file_path=None, file_type="")) == ""
    p = ItakuPoster()
    assert p.validate(_pkg(tmp_path)) == []
    monkeypatch.setattr(ItakuPoster, "max_video_size", 10)
    errs = p.validate(_pkg(tmp_path))
    assert len(errs) == 1 and "up to 500 MB" in errs[0]
    # an image is still held to 10 MB, not the video cap
    monkeypatch.setattr(ItakuPoster, "max_file_size", 10)
    pic = tmp_path / "pic.png"
    pic.write_bytes(b"\x89PNG" + b"\x00" * 20)
    assert "max 10MB" in p.validate(_pkg(tmp_path, file_path=str(pic), file_type="png", media_kind="image"))[0]


def test_audio_is_refused_before_the_network(tmp_path):
    from posting.manager import _get_poster
    track = tmp_path / "t.mp3"
    track.write_bytes(b"ID3")
    pkg = _pkg(tmp_path, file_path=str(track), file_type="mp3", media_kind="audio")
    refusal = _get_poster("ik").media_refusal(pkg)
    assert refusal and refusal.startswith("Itaku doesn't take mp3 audio")
