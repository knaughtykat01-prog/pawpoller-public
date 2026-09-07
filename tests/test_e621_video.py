"""e621 webm — MEDIATYPES phase 2, site 3 (4.19.1).

e621 takes video as webm only, through the same ``upload[file]`` field as an image, so the
existing upload path carries it; what this pins is that a webm package really reaches that field
with a video MIME, that an mp4 is refused before the network, and that the size cap still applies.
The network is faked; the "video" is a few bytes behind a .webm name.
"""
from __future__ import annotations

import asyncio

from clients.e621.client import E621Client
from posting.platforms.base import StoryUploadPackage
from posting.platforms.e621 import E621Poster


class _Resp:
    status_code = 200
    content = b"{}"

    def json(self):
        return {"success": True, "post_id": 4242, "location": "/posts/4242"}


class _FakeHTTP:
    def __init__(self):
        self.calls = []

    async def post(self, url, data=None, files=None, headers=None, auth=None, timeout=None):
        self.calls.append({"url": url, "data": dict(data or {}), "files": {k: (v[0], v[2]) for k, v in (files or {}).items()},
                           "timeout": timeout})
        return _Resp()


def _pkg(path, ft, kind, **over):
    kw = dict(story_name="Sample_Clip", chapter_index=0, chapter_title="", platform="e621",
              title="Sample Clip", description="a loop", tags=["a", "b", "c", "d", "e"], rating="general",
              file_path=str(path), file_type=ft, media_kind=kind)
    kw.update(over)
    return StoryUploadPackage(**kw)


def test_a_webm_reaches_upload_file_with_a_video_mime_and_the_long_timeout(tmp_path, monkeypatch):
    clip = tmp_path / "clip.webm"
    clip.write_bytes(b"\x1a\x45\xdf\xa3" + b"\x00" * 50)
    c = E621Client(username="owner", api_key="k")
    c._http = _FakeHTTP()
    r = asyncio.run(c.upload_post(tag_string="a b c d e", rating="s", file_path=str(clip)))
    call = c._http.calls[0]
    assert call["url"].endswith("/uploads.json")
    assert call["files"]["upload[file]"] == ("clip.webm", "video/webm")
    assert call["timeout"] >= 600
    assert r["post_id"] == "4242" and r["url"].endswith("/posts/4242")
    pic = tmp_path / "pic.png"
    pic.write_bytes(b"\x89PNG")
    asyncio.run(c.upload_post(tag_string="a b c d e", rating="s", file_path=str(pic)))
    assert c._http.calls[1]["files"]["upload[file]"] == ("pic.png", "image/png") and c._http.calls[1]["timeout"] == 120.0


def test_mp4_and_audio_are_refused_before_the_network_but_webm_is_not(tmp_path):
    from posting.manager import _get_poster
    p = _get_poster("e621")
    for name, ft, kind in (("clip.mp4", "mp4", "video"), ("track.mp3", "mp3", "audio")):
        f = tmp_path / name
        f.write_bytes(b"\x00" * 10)
        refusal = p.media_refusal(_pkg(f, ft, kind))
        assert refusal and refusal.startswith(f"e621 doesn't take {ft} {kind}"), refusal
    clip = tmp_path / "clip.webm"
    clip.write_bytes(b"\x00" * 10)
    assert p.media_refusal(_pkg(clip, "webm", "video")) is None
    assert E621Poster().validate(_pkg(clip, "webm", "video")) == []


def test_the_size_cap_covers_a_webm_too(tmp_path, monkeypatch):
    clip = tmp_path / "clip.webm"
    clip.write_bytes(b"\x00" * 200)
    monkeypatch.setattr(E621Poster, "max_file_size", 100)
    errs = E621Poster().validate(_pkg(clip, "webm", "video"))
    assert len(errs) == 1 and errs[0].startswith("File too large")
