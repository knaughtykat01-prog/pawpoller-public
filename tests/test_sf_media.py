"""SoFurry music / video — MEDIATYPES phase 2, site 6 (4.19.3).

The same single-item create flow as artwork, filed under SoFurry's Music (40 / 41 Track) or Video
(50 / 59 Other) category; the official API has no thumbnail route, so the poster is not sent. The
network is faked; no media file enters the repo.
"""
from __future__ import annotations

import asyncio

import pytest

from posting.platforms.base import StoryUploadPackage
from posting.platforms.sofurry import SoFurryPoster, _media_kind


class _FakeClient:
    def __init__(self):
        self.calls = []

    async def create_submission(self, file_path, **kw):
        self.calls.append({"file": file_path, **kw})
        return {"submission_id": "naOMVbXe", "url": "https://www.sofurry.com/view/naOMVbXe"}


def _pkg(path, ft, kind, **over):
    kw = dict(story_name="Sample_Track", chapter_index=0, chapter_title="", platform="sf",
              title="Sample Track", description="a tune", tags=["a", "b"], rating="general",
              file_path=str(path), file_type=ft, media_kind=kind, thumbnail_path="")
    kw.update(over)
    return StoryUploadPackage(**kw)


@pytest.fixture
def poster(monkeypatch):
    p = SoFurryPoster()
    client = _FakeClient()

    async def _ensure():
        return client
    monkeypatch.setattr(p, "_ensure_client", _ensure)
    monkeypatch.setattr(p, "_cleanup_tmp_files", lambda: None)
    p._fake = client
    return p


def test_an_mp3_is_a_music_track_and_never_loads_a_story(poster, tmp_path, monkeypatch):
    from posting import story_reader
    monkeypatch.setattr(story_reader, "load_story", lambda name: (_ for _ in ()).throw(AssertionError("no story load")))
    track = tmp_path / "track.mp3"
    track.write_bytes(b"ID3")
    r = asyncio.run(poster.post(_pkg(track, "mp3", "audio", rating="adult")))
    assert r.success and r.external_id == "naOMVbXe"
    call = poster._fake.calls[0]
    assert call["category"] == 40 and call["sub_type"] == 41 and call["rating"] == 20 and call["privacy"] == 3
    assert call["title"] == "Sample Track" and call["tags"] == ["a", "b"]


def test_a_video_is_video_other_and_the_piece_can_name_its_type_and_privacy(poster, tmp_path):
    clip = tmp_path / "clip.webm"
    clip.write_bytes(b"\x00" * 8)
    asyncio.run(poster.post(_pkg(clip, "webm", "video")))
    assert poster._fake.calls[0]["category"] == 50 and poster._fake.calls[0]["sub_type"] == 59
    asyncio.run(poster.post(_pkg(clip, "webm", "video", extra={"sub_type": "51", "draft": True})))
    assert poster._fake.calls[1]["sub_type"] == 51 and poster._fake.calls[1]["privacy"] == 1


def test_media_kind_by_library_kind_or_extension(tmp_path):
    assert _media_kind(_pkg("x.mp3", "mp3", "")) == "audio"          # pre-4.18.0 package
    assert _media_kind(_pkg("x.mp4", "mp4", "video")) == "video"
    assert _media_kind(_pkg("x.png", "png", "image")) == "" and _media_kind(_pkg("x.html", "html", "")) == ""


def test_media_gets_the_100_mb_cap_text_keeps_512_kb(tmp_path, monkeypatch):
    p = SoFurryPoster()
    track = tmp_path / "track.mp3"
    track.write_bytes(b"\x00" * 200)
    assert p.validate(_pkg(track, "mp3", "audio")) == []
    monkeypatch.setattr(SoFurryPoster, "_MEDIA_MAX", 100)
    errs = p.validate(_pkg(track, "mp3", "audio"))
    assert len(errs) == 1 and "100MB" in errs[0]
    doc = tmp_path / "story.html"
    doc.write_bytes(b"<p>" + b"x" * 300)
    monkeypatch.setattr(SoFurryPoster, "max_file_size", 100)
    assert "512KB" in p.validate(_pkg(doc, "html", ""))[0]


def test_wav_and_mov_refused_before_the_network_mp3_mp4_webm_not(tmp_path):
    from posting.manager import _get_poster
    p = _get_poster("sf")
    for name, ft, kind in (("t.wav", "wav", "audio"), ("c.mov", "mov", "video")):
        f = tmp_path / name
        f.write_bytes(b"\x00")
        assert p.media_refusal(_pkg(f, ft, kind)).startswith(f"SoFurry doesn't take {ft} {kind}")
    for name, ft, kind in (("t.mp3", "mp3", "audio"), ("c.mp4", "mp4", "video"), ("c.webm", "webm", "video")):
        f = tmp_path / name
        f.write_bytes(b"\x00")
        assert p.media_refusal(_pkg(f, ft, kind)) is None


def test_the_client_labels_media_content_and_waits_longer(tmp_path):
    from clients.sf.client import SoFurryClient

    class _Resp:
        status_code = 200
        text = '{"contentId": "c1"}'

        def json(self):
            return {"contentId": "c1"}

    class _API:
        def __init__(self):
            self.calls = []

        async def post(self, path, files=None, timeout=None, **kw):
            self.calls.append({"path": path, "files": {k: (v[0], v[2]) for k, v in files.items()}, "timeout": timeout})
            return _Resp()

    c = SoFurryClient.__new__(SoFurryClient)
    c._api = _API()

    async def _ok():
        return None
    c._require_token = _ok
    c._check = lambda resp, what: resp.json()
    track = tmp_path / "track.mp3"
    track.write_bytes(b"\x00" * 2048)
    asyncio.run(c.upload_content("s1", str(track)))
    call = c._api.calls[0]
    assert call["files"]["file"] == ("track.mp3", "audio/mpeg") and call["timeout"] >= 600
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"\x00" * 2048)
    asyncio.run(c.upload_content("s1", str(clip)))
    assert c._api.calls[1]["files"]["file"] == ("clip.mp4", "video/mp4")
