"""Weasyl audio — MEDIATYPES phase 2, site 7 (4.19.3).

Weasyl's third submit form, ``/submit/multimedia``, takes an mp3 as ``submitfile`` with the poster as
both ``coverfile`` and ``thumbfile`` and a multimedia subtype (3010 Original Music by default). Weasyl
takes no video files. The network is faked; no media file enters the repo.
"""
from __future__ import annotations

import asyncio

from clients.weasyl.client import WeasylClient, _image_mime
from posting.platforms.base import StoryUploadPackage
from posting.platforms.weasyl import WeasylPoster, _is_audio


class _FakeClient:
    def __init__(self):
        self.calls = []

    async def submit_multimedia(self, file_path, **kw):
        self.calls.append({"method": "submit_multimedia", "file": file_path, **kw})
        return {"submission_id": "2468", "url": "https://www.weasyl.com/submission/2468/sample-track"}

    async def submit_visual(self, file_path, **kw):
        self.calls.append({"method": "submit_visual", "file": file_path, **kw})
        return {"submission_id": "1", "url": "https://www.weasyl.com/submission/1"}

    async def submit_literary(self, file_path, **kw):
        self.calls.append({"method": "submit_literary", "file": file_path, **kw})
        return {"submission_id": "2", "url": "https://www.weasyl.com/submission/2"}


def _pkg(path, ft, kind, **over):
    kw = dict(story_name="Sample_Track", chapter_index=0, chapter_title="", platform="ws",
              title="Sample Track", description="a tune", tags=["a", "b"], rating="mature",
              file_path=str(path), file_type=ft, media_kind=kind, thumbnail_path="")
    kw.update(over)
    return StoryUploadPackage(**kw)


def _poster(fake, monkeypatch):
    p = WeasylPoster()

    async def _ensure():
        return fake
    monkeypatch.setattr(p, "_ensure_client", _ensure)
    return p


def test_an_mp3_is_a_multimedia_submission_with_the_poster_as_cover(tmp_path, monkeypatch):
    track = tmp_path / "track.mp3"
    track.write_bytes(b"ID3")
    poster = tmp_path / "poster.png"
    poster.write_bytes(b"\x89PNG")
    fake = _FakeClient()
    r = asyncio.run(_poster(fake, monkeypatch).post(_pkg(track, "mp3", "audio", thumbnail_path=str(poster))))
    assert r.success and r.external_id == "2468"
    call = fake.calls[0]
    assert call["method"] == "submit_multimedia" and call["subtype"] == 3010 and call["cover_path"] == str(poster)
    assert call["rating"] == 30 and call["tags"] == "a b"
    asyncio.run(_poster(fake, monkeypatch).post(_pkg(track, "mp3", "audio", extra={"subtype": "3040"})))
    assert fake.calls[1]["subtype"] == 3040


def test_pictures_and_text_are_unchanged(tmp_path, monkeypatch):
    pic = tmp_path / "pic.png"
    pic.write_bytes(b"\x89PNG")
    doc = tmp_path / "story.txt"
    doc.write_text("once")
    fake = _FakeClient()
    p = _poster(fake, monkeypatch)
    asyncio.run(p.post(_pkg(pic, "png", "image")))
    asyncio.run(p.post(_pkg(doc, "txt", "")))
    assert [c["method"] for c in fake.calls] == ["submit_visual", "submit_literary"]


def test_is_audio_and_the_15_mb_cap(tmp_path, monkeypatch):
    assert _is_audio(_pkg("t.mp3", "mp3", "")) and not _is_audio(_pkg("p.png", "png", "image"))
    track = tmp_path / "track.mp3"
    track.write_bytes(b"\x00" * 200)
    p = WeasylPoster()
    assert p.validate(_pkg(track, "mp3", "audio")) == []
    monkeypatch.setattr(WeasylPoster, "max_audio_size", 100)
    errs = p.validate(_pkg(track, "mp3", "audio"))
    assert len(errs) == 1 and "up to 15 MB" in errs[0]


def test_video_and_wav_refused_before_the_network_mp3_not(tmp_path):
    from posting.manager import _get_poster
    p = _get_poster("ws")
    for name, ft, kind in (("c.mp4", "mp4", "video"), ("t.wav", "wav", "audio")):
        f = tmp_path / name
        f.write_bytes(b"\x00")
        assert p.media_refusal(_pkg(f, ft, kind)).startswith(f"Weasyl doesn't take {ft} {kind}")
    t = tmp_path / "t.mp3"
    t.write_bytes(b"\x00")
    assert p.media_refusal(_pkg(t, "mp3", "audio")) is None


def test_the_client_posts_the_multimedia_form_with_cover_and_thumb(tmp_path):
    class _Resp:
        status_code = 200
        url = "https://www.weasyl.com/submission/2468/sample-track"
        text = ""

    class _HTTP:
        def __init__(self):
            self.calls = []

        async def post(self, url, data=None, files=None, timeout=None, follow_redirects=False):
            self.calls.append({"url": url, "data": dict(data or {}), "files": {k: (v[0], v[2]) for k, v in files.items()},
                               "timeout": timeout})
            return _Resp()

    c = WeasylClient.__new__(WeasylClient)
    c._http = _HTTP()

    async def _csrf(url):
        return "tok123"
    c._get_csrf_token = _csrf
    track = tmp_path / "track.mp3"
    track.write_bytes(b"ID3")
    poster = tmp_path / "track-poster.jpg"
    poster.write_bytes(b"\xff\xd8\xff")
    r = asyncio.run(c.submit_multimedia(str(track), title="Sample Track", tags="a b", rating=30, cover_path=str(poster)))
    call = c._http.calls[0]
    assert call["url"] == "https://www.weasyl.com/submit/multimedia"
    assert call["data"]["subtype"] == "3010" and call["data"]["token"] == "tok123" and call["data"]["rating"] == "30"
    assert call["files"]["submitfile"] == ("track.mp3", "audio/mpeg")
    assert call["files"]["coverfile"] == ("track-poster.jpg", "image/jpeg") and call["files"]["thumbfile"] == ("track-poster.jpg", "image/jpeg")
    assert call["timeout"] >= 300
    assert r == {"submission_id": "2468", "url": "https://www.weasyl.com/submission/2468/sample-track"}
    assert _image_mime("a.png") == "image/png" and _image_mime("a.webp") == "image/webp"
