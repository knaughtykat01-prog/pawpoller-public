"""DeviantArt film — MEDIATYPES phase 3, site 2 (4.20.1).

An mp4 / mov goes through the same two Sta.sh calls as an image (stash/submit with a video MIME,
then stash/publish). Whether the OAuth stash endpoint takes a video at all is unverified — the
first live attempt settles it. The network is faked; no media file enters the repo.
"""
from __future__ import annotations

import asyncio

from posting.platforms.base import StoryUploadPackage
from posting.platforms.deviantart import DeviantArtPoster, _is_film


class _FakeClient:
    def __init__(self):
        self.calls = []

    async def oauth_stash_submit(self, file_path, **kw):
        self.calls.append(("submit", file_path, kw))
        return {"itemid": 777, "stackid": 1}

    async def oauth_stash_publish(self, itemid, **kw):
        self.calls.append(("publish", itemid, kw))
        return {"deviationid": "uuid-1", "url": "https://www.deviantart.com/owner/art/sample-clip-123456789"}

    async def oauth_create_literature(self, **kw):
        self.calls.append(("literature", kw))
        return {"deviationid": "uuid-2", "url": "https://www.deviantart.com/owner/art/story-2"}


def _pkg(path, ft, kind, **over):
    kw = dict(story_name="Sample_Clip", chapter_index=0, chapter_title="", platform="da",
              title="Sample Clip", description="a loop", tags=["loop"], rating="general",
              file_path=str(path), file_type=ft, media_kind=kind)
    kw.update(over)
    return StoryUploadPackage(**kw)


def _poster(fake, monkeypatch):
    import config
    p = DeviantArtPoster()

    async def _ensure():
        return fake, "tok"
    monkeypatch.setattr(p, "_ensure_client", _ensure)
    monkeypatch.setattr(config, "get_settings", lambda: {"artwork_da_catpath": "film/other"})
    return p


def test_a_video_is_stashed_and_published_like_an_image(tmp_path, monkeypatch):
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"\x00\x00\x00\x18ftypisom" + b"\x00" * 20)
    fake = _FakeClient()
    r = asyncio.run(_poster(fake, monkeypatch).post(_pkg(clip, "mp4", "video")))
    assert r.success and r.external_id == "123456789"
    assert fake.calls[0][0] == "submit" and fake.calls[0][1] == str(clip) and fake.calls[0][2]["title"] == "Sample Clip"
    assert fake.calls[1][0] == "publish" and fake.calls[1][1] == 777 and fake.calls[1][2]["catpath"] == "film/other"


def test_stash_submit_labels_a_video_and_waits_longer(tmp_path):
    from clients.da.client import DAClient

    class _Resp:
        status_code = 200
        text = ""

        def json(self):
            return {"itemid": 5}

    class _HTTP:
        def __init__(self):
            self.calls = []

        async def post(self, url, data=None, files=None, timeout=None):
            self.calls.append({"url": url, "files": {k: (v[0], v[2]) for k, v in files.items()}, "timeout": timeout})
            return _Resp()

    c = DAClient.__new__(DAClient)
    c._http = _HTTP()
    clip = tmp_path / "clip.mov"
    clip.write_bytes(b"\x00" * 8)
    asyncio.run(c.oauth_stash_submit(str(clip), title="Sample Clip", tags=["loop"], access_token="tok"))
    call = c._http.calls[0]
    assert call["files"]["file"] == ("clip.mov", "video/quicktime") and call["timeout"] >= 600
    pic = tmp_path / "pic.png"
    pic.write_bytes(b"\x89PNG")
    asyncio.run(c.oauth_stash_submit(str(pic), title="Pic", tags=["a"], access_token="tok"))
    assert c._http.calls[1]["files"]["file"] == ("pic.png", "image/png") and c._http.calls[1]["timeout"] == 120.0


def test_is_film_and_the_cap(tmp_path, monkeypatch):
    assert _is_film(_pkg("c.mp4", "mp4", "video")) and _is_film(_pkg("c.mov", "mov", "video"))
    assert not _is_film(_pkg("c.webm", "webm", "video")) and not _is_film(_pkg("p.png", "png", "image"))
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"\x00" * 200)
    p = DeviantArtPoster()
    assert p.validate(_pkg(clip, "mp4", "video")) == []
    monkeypatch.setattr(DeviantArtPoster, "max_film_size", 100)
    errs = p.validate(_pkg(clip, "mp4", "video"))
    assert len(errs) == 1 and "up to 200 MB" in errs[0]


def test_webm_and_audio_refused_before_the_network_mp4_mov_not(tmp_path):
    from posting.manager import _get_poster
    p = _get_poster("da")
    for name, ft, kind in (("c.webm", "webm", "video"), ("t.mp3", "mp3", "audio")):
        f = tmp_path / name
        f.write_bytes(b"\x00")
        assert p.media_refusal(_pkg(f, ft, kind)).startswith(f"DeviantArt doesn't take {ft} {kind}")
    for name, ft in (("c.mp4", "mp4"), ("c.mov", "mov")):
        g = tmp_path / name
        g.write_bytes(b"\x00")
        assert p.media_refusal(_pkg(g, ft, "video")) is None
