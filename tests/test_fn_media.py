"""FurryNetwork multimedia — MEDIATYPES phase 2, site 8 (4.19.4).

A video / audio piece goes into FurryNetwork's multimedia collection through the same resumable
upload + metadata PATCH as artwork, on the sibling paths. Built by analogy with the artwork flow —
to verify on the site. The network is faked; no media file enters the repo.
"""
from __future__ import annotations

import asyncio

import config
from posting.platforms.base import StoryUploadPackage
import posting.platforms.furrynetwork as fnp


class _FakeFn:
    def __init__(self, **kw):
        self.username = kw.get("username", "")
        self.password = kw.get("password", "")
        self.refresh_token = kw.get("refresh_token", "")
        self.access_token = kw.get("access_token", "")
        self.calls = []

    async def login(self):
        return True

    async def get_characters(self):
        return [{"name": "kit", "default": True}]

    async def upload_artwork(self, **kw):
        self.calls.append(("artwork", kw))
        return {"success": True, "id": "555", "url": "https://furrynetwork.com/kit/artwork/555"}

    async def upload_multimedia(self, **kw):
        self.calls.append(("multimedia", kw))
        return {"success": True, "id": "777", "url": "https://furrynetwork.com/kit/multimedia/777"}


def _pkg(path, ft, kind, **over):
    base = dict(story_name="Sample_Clip", chapter_index=0, chapter_title="", platform="fn",
                title="Sample Clip", description="a loop", tags=["wolf", "loop"], rating="general",
                file_path=str(path), file_type=ft, media_kind=kind)
    base.update(over)
    return StoryUploadPackage(**base)


def test_a_video_goes_to_the_multimedia_collection(tmp_path, monkeypatch):
    config.save_settings({"fn_username": "owner@example.com", "fn_password": "pw"})
    monkeypatch.setattr(fnp, "FnClient", _FakeFn)
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"\x00" * 8)
    poster = fnp.FurryNetworkPoster()
    r = asyncio.run(poster.post(_pkg(clip, "mp4", "video")))
    assert r.success and r.external_id == "777" and "kit/multimedia/777" in r.external_url
    coll, kw = poster._client.calls[0]
    assert coll == "multimedia" and kw["character"] == "kit" and kw["title"] == "Sample Clip"
    pic = tmp_path / "pic.png"
    pic.write_bytes(b"\x89PNG")
    asyncio.run(poster.post(_pkg(pic, "png", "image")))
    assert poster._client.calls[1][0] == "artwork"


def test_is_media_and_the_200_mb_cap(tmp_path, monkeypatch):
    assert fnp._is_media(_pkg("t.mp3", "mp3", "")) and fnp._is_media(_pkg("c.mp4", "mp4", "video"))
    assert not fnp._is_media(_pkg("p.png", "png", "image"))
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"\x00" * 200)
    p = fnp.FurryNetworkPoster()
    assert p.validate(_pkg(clip, "mp4", "video")) == []
    monkeypatch.setattr(fnp.FurryNetworkPoster, "max_media_size", 100)
    errs = p.validate(_pkg(clip, "mp4", "video"))
    assert len(errs) == 1 and "up to 200 MB" in errs[0] and errs[0].startswith("Video is")
    monkeypatch.setattr(fnp.FurryNetworkPoster, "max_file_size", 100)
    pic = tmp_path / "pic.png"
    pic.write_bytes(b"\x89PNG" + b"\x00" * 200)
    assert "File too large" in p.validate(_pkg(pic, "png", "image"))[0]


def test_webm_and_wav_refused_before_the_network_mp4_mp3_not(tmp_path):
    from posting.manager import _get_poster
    p = _get_poster("fn")
    for name, ft, kind in (("c.webm", "webm", "video"), ("t.wav", "wav", "audio")):
        f = tmp_path / name
        f.write_bytes(b"\x00")
        assert p.media_refusal(_pkg(f, ft, kind)).startswith(f"FurryNetwork doesn't take {ft} {kind}")
    for name, ft, kind in (("c.mp4", "mp4", "video"), ("t.mp3", "mp3", "audio")):
        f = tmp_path / name
        f.write_bytes(b"\x00")
        assert p.media_refusal(_pkg(f, ft, kind)) is None


def test_the_client_uses_the_collection_in_every_path(tmp_path):
    from clients.fn.client import FnClient

    class _Resp:
        def __init__(self, code, body=None):
            self.status_code = code
            self._body = body or {}
            self.text = ""

        def json(self):
            return self._body

    class _Up:
        calls = []

        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, params=None, content=None, headers=None):
            _Up.calls.append({"url": url, "params": dict(params or {})})
            return _Resp(200, {"id": 777})

    class _Http:
        calls = []

        async def patch(self, url, json=None, headers=None):
            _Http.calls.append({"url": url, "json": json})
            return _Resp(200, {})

    import clients.fn.client as fnc
    _Up.calls, _Http.calls = [], []
    c = FnClient.__new__(FnClient)
    c.access_token = "tok"

    async def _tok():
        return None
    c._ensure_token = _tok
    c._http = lambda: _Http()
    orig = fnc.httpx.AsyncClient
    fnc.httpx.AsyncClient = _Up
    try:
        clip = tmp_path / "clip.mp4"
        clip.write_bytes(b"\x00" * 100)
        r = asyncio.run(c.upload_multimedia(character="kit", file_path=str(clip), title="Sample Clip", tags=["wolf"]))
    finally:
        fnc.httpx.AsyncClient = orig
    assert r == {"success": True, "id": "777", "url": "https://furrynetwork.com/kit/multimedia/777"}
    assert _Up.calls[0]["url"].endswith("/submission/kit/multimedia/upload")
    assert _Http.calls[0]["url"].endswith("/multimedia/777") and _Http.calls[0]["json"]["title"] == "Sample Clip"
    r = asyncio.run(c.upload_submission("story", character="kit", file_path=str(clip), title="x"))
    assert not r["success"]
