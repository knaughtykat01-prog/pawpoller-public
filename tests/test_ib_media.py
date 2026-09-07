"""Inkbunny mp4 / mp3 — MEDIATYPES phase 2, site 4 (4.19.2).

The file goes through the same ``uploadedfile[0]`` as a picture with the Library's poster as the
custom thumbnail, and the metadata step files it under Inkbunny's media submission type (10 video,
11 single track) unless the piece names its own. The network is faked; no media file enters the repo.
"""
from __future__ import annotations

import asyncio

from clients.ib.client import InkbunnyClient, _thumb_mime
from posting.platforms.base import StoryUploadPackage
from posting.platforms.inkbunny import InkbunnyPoster, _media_kind


class _Resp:
    status_code = 200
    content = b'{"submission_id": "777"}'

    def raise_for_status(self):
        pass

    def json(self):
        return {"submission_id": "777"}


class _FakeHTTP:
    def __init__(self):
        self.calls = []

    async def post(self, url, data=None, files=None, timeout=None):
        self.calls.append({"url": url.rsplit("/", 1)[-1], "data": dict(data or {}),
                           "files": {k: (v[0], v[2] if len(v) > 2 else None) for k, v in (files or {}).items()},
                           "timeout": timeout})
        return _Resp()


class _FakeClient:
    """Records the poster's two calls the way the real client would receive them."""
    def __init__(self):
        self.sid = "sid"
        self.uploads = []
        self.edits = []

    async def upload_submission(self, file_path, *, submission_type="4", thumbnail_path=None):
        self.uploads.append({"file": file_path, "submission_type": submission_type, "thumbnail": thumbnail_path})
        return 777

    async def edit_submission(self, submission_id, **kw):
        self.edits.append({"id": submission_id, **kw})
        return {}


def _pkg(path, ft, kind, **over):
    kw = dict(story_name="Sample_Track", chapter_index=0, chapter_title="", platform="ib",
              title="Sample Track", description="a tune", tags=["a", "b", "c", "d"], rating="general",
              file_path=str(path), file_type=ft, media_kind=kind, thumbnail_path="")
    kw.update(over)
    return StoryUploadPackage(**kw)


def _poster(fake, monkeypatch):
    p = InkbunnyPoster()

    async def _ensure():
        return fake
    monkeypatch.setattr(p, "_ensure_client", _ensure)
    return p


def test_an_mp3_uploads_with_the_poster_and_files_as_a_single_track(tmp_path, monkeypatch):
    track = tmp_path / "track.mp3"
    track.write_bytes(b"ID3")
    poster = tmp_path / "poster.png"
    poster.write_bytes(b"\x89PNG")
    fake = _FakeClient()
    r = asyncio.run(_poster(fake, monkeypatch).post(_pkg(track, "mp3", "audio", thumbnail_path=str(poster))))
    assert r.success and r.external_id == "777" and r.external_url.endswith("/s/777")
    assert fake.uploads[0] == {"file": str(track), "submission_type": "3", "thumbnail": str(poster)}
    assert fake.edits[0]["type"] == "11" and fake.edits[0]["title"] == "Sample Track"


def test_an_mp4_files_as_a_video_and_the_piece_can_name_its_own_type(tmp_path, monkeypatch):
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"\x00" * 8)
    fake = _FakeClient()
    p = _poster(fake, monkeypatch)
    asyncio.run(p.post(_pkg(clip, "mp4", "video")))
    assert fake.uploads[0]["submission_type"] == "1" and fake.edits[0]["type"] == "10"
    asyncio.run(p.post(_pkg(clip, "mp4", "video", extra={"type": "9"})))
    assert fake.edits[1]["type"] == "9"


def test_a_picture_and_a_story_send_no_type(tmp_path, monkeypatch):
    pic = tmp_path / "pic.png"
    pic.write_bytes(b"\x89PNG")
    fake = _FakeClient()
    asyncio.run(_poster(fake, monkeypatch).post(_pkg(pic, "png", "image")))
    assert "type" not in fake.edits[0] and fake.uploads[0]["submission_type"] == "1"


def test_media_kind_by_library_kind_or_extension(tmp_path):
    assert _media_kind(_pkg("x.mp4", "mp4", "")) == "video"          # pre-4.18.0 package
    assert _media_kind(_pkg("x.mp3", "mp3", "audio")) == "audio"
    assert _media_kind(_pkg("x.png", "png", "image")) == "" and _media_kind(_pkg("x.txt", "bbcode", "")) == ""


def test_webm_wav_refused_before_the_network_mp4_mp3_not(tmp_path):
    from posting.manager import _get_poster
    p = _get_poster("ib")
    for name, ft, kind in (("c.webm", "webm", "video"), ("t.wav", "wav", "audio")):
        f = tmp_path / name
        f.write_bytes(b"\x00")
        assert p.media_refusal(_pkg(f, ft, kind)).startswith(f"Inkbunny doesn't take {ft} {kind}")
    for name, ft, kind in (("c.mp4", "mp4", "video"), ("t.mp3", "mp3", "audio")):
        f = tmp_path / name
        f.write_bytes(b"\x00")
        assert p.media_refusal(_pkg(f, ft, kind)) is None


def test_the_client_sends_type_and_labels_a_jpeg_thumbnail_honestly(tmp_path):
    c = InkbunnyClient.__new__(InkbunnyClient)
    c.sid = "sid"
    c._http = _FakeHTTP()
    asyncio.run(c.edit_submission(777, title="T", type="11"))
    assert c._http.calls[0]["url"] == "api_editsubmission.php" and c._http.calls[0]["data"]["type"] == "11"
    asyncio.run(c.edit_submission(777, title="T"))
    assert "type" not in c._http.calls[1]["data"]
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"\x00" * 8)
    poster = tmp_path / "clip-poster.jpg"
    poster.write_bytes(b"\xff\xd8\xff")
    asyncio.run(c.upload_submission(str(clip), submission_type="1", thumbnail_path=str(poster)))
    up = c._http.calls[2]
    assert up["url"] == "api_upload.php" and up["files"]["uploadedthumbnail[0]"] == ("clip-poster.jpg", "image/jpeg")
    assert up["timeout"] >= 600
    assert _thumb_mime("a.png") == "image/png" and _thumb_mime("a.JPG") == "image/jpeg"
