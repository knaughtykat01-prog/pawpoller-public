"""X video — MEDIATYPES phase 2, site 9 (4.19.4).

X's v1.1 media/upload takes a video as INIT → APPEND (segments) → FINALIZE, then STATUS until
processing succeeds; the tweet then carries the media id like an image. mp4 / mov only, ≤ 512 MB,
≤ 140 s — checked from the Library's measurements before anything is uploaded. Audio is still
announced with its poster. The network is faked; no media file enters the repo.
"""
from __future__ import annotations

import asyncio

import pytest

from clients.tw.client import TWClient
from posting.platforms.base import StoryUploadPackage
from posting.platforms.twitter import TwitterPoster, _is_video


class _Resp:
    def __init__(self, code, body):
        self.status_code = code
        self._body = body
        self.text = ""

    def json(self):
        return self._body


class _FakeHTTP:
    """Plays X's upload protocol: INIT gives an id, APPEND 204s, FINALIZE says
    pending, then STATUS goes in_progress → succeeded."""
    def __init__(self, fail_processing=False):
        self.calls = []
        self.status_polls = 0
        self.fail_processing = fail_processing

    async def post(self, url, data=None, files=None, headers=None, timeout=None):
        cmd = (data or {}).get("command")
        entry = {"cmd": cmd, "data": dict(data or {}), "timeout": timeout}
        if files:
            entry["blob"] = len(files["media"][1])
        self.calls.append(entry)
        if cmd == "INIT":
            return _Resp(202, {"media_id_string": "9001"})
        if cmd == "APPEND":
            return _Resp(204, {})
        if cmd == "FINALIZE":
            return _Resp(200, {"media_id_string": "9001", "processing_info": {"state": "pending", "check_after_secs": 0}})
        raise AssertionError(cmd)

    async def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append({"cmd": "STATUS", "data": dict(params or {})})
        self.status_polls += 1
        if self.status_polls == 1:
            return _Resp(200, {"processing_info": {"state": "in_progress", "check_after_secs": 0}})
        if self.fail_processing:
            return _Resp(200, {"processing_info": {"state": "failed", "error": {"message": "Unsupported codec"}}})
        return _Resp(200, {"processing_info": {"state": "succeeded"}})


def _client(http):
    c = TWClient.__new__(TWClient)
    c.auth_token, c.ct0 = "a", "c"
    c.last_error = ""
    c._http = http

    async def _hdrs(method, url):
        return {"x-test": method}
    c._write_headers = _hdrs
    return c


def test_the_chunked_protocol_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setattr(TWClient, "VIDEO_SEGMENT", 100)
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"\x00" * 250)
    http = _FakeHTTP()
    mid = asyncio.run(_client(http).upload_video(str(clip)))
    assert mid == "9001"
    cmds = [c["cmd"] for c in http.calls]
    assert cmds == ["INIT", "APPEND", "APPEND", "APPEND", "FINALIZE", "STATUS", "STATUS"]
    init = http.calls[0]["data"]
    assert init == {"command": "INIT", "total_bytes": "250", "media_type": "video/mp4", "media_category": "tweet_video"}
    assert [c["blob"] for c in http.calls[1:4]] == [100, 100, 50]
    assert [c["data"]["segment_index"] for c in http.calls[1:4]] == ["0", "1", "2"]
    assert http.calls[1]["timeout"] >= 300


def test_a_processing_failure_reports_xs_own_words(tmp_path):
    clip = tmp_path / "clip.mov"
    clip.write_bytes(b"\x00" * 10)
    c = _client(_FakeHTTP(fail_processing=True))
    assert asyncio.run(c.upload_video(str(clip))) is None
    assert "Unsupported codec" in c.last_error


def _pkg(path, ft, kind, **over):
    kw = dict(story_name="Sample_Clip", chapter_index=0, chapter_title="", platform="tw",
              title="Sample Clip", description="a loop", tags=["loop"], rating="general",
              file_path=str(path), file_type=ft, media_kind=kind, thumbnail_path="")
    kw.update(over)
    return StoryUploadPackage(**kw)


class _FakeClient:
    def __init__(self, **kw):
        self.last_error = ""
        self.calls = []

    async def upload_video(self, path):
        self.calls.append(("video", path))
        return "9001"

    async def upload_media(self, path):
        self.calls.append(("image", path))
        return "1"

    async def set_media_alt(self, *a):
        return True

    async def create_tweet(self, text, media_ids=None, *, sensitive=False):
        self.calls.append(("tweet", text, list(media_ids or []), sensitive))
        return {"id": "42", "url": "https://x.com/owner/status/42"}

    async def close(self):
        pass


@pytest.fixture
def poster(monkeypatch):
    p = TwitterPoster()
    monkeypatch.setattr(p, "_creds", lambda *a, **k: ("tok", "ct0", "owner"))

    async def _ok(client):
        return ""
    monkeypatch.setattr(p, "_wrong_session", _ok)
    holder = {}

    def _mk(**kw):
        holder["c"] = _FakeClient(**kw)
        return holder["c"]
    monkeypatch.setattr("clients.tw.client.TWClient", _mk)
    p._holder = holder
    return p


def test_a_video_piece_is_uploaded_itself(poster, tmp_path):
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"\x00" * 10)
    poster_img = tmp_path / "poster.jpg"
    poster_img.write_bytes(b"\xff\xd8\xff")
    r = asyncio.run(poster.post(_pkg(clip, "mp4", "video", thumbnail_path=str(poster_img))))
    assert r.success and r.external_id == "42"
    calls = poster._holder["c"].calls
    assert calls[0] == ("video", str(clip))
    assert calls[1][0] == "tweet" and calls[1][2] == ["9001"] and "#loop" in calls[1][1]


def test_an_audio_piece_is_still_announced_with_its_poster(poster, tmp_path):
    track = tmp_path / "track.mp3"
    track.write_bytes(b"ID3")
    poster_img = tmp_path / "poster.png"
    poster_img.write_bytes(b"\x89PNG")
    r = asyncio.run(poster.post(_pkg(track, "mp3", "audio", thumbnail_path=str(poster_img))))
    assert r.success
    assert poster._holder["c"].calls[0] == ("image", str(poster_img))


def test_is_video_and_the_caps(tmp_path, monkeypatch):
    assert _is_video(_pkg("c.mp4", "mp4", "video")) and _is_video(_pkg("c.mov", "mov", "video"))
    assert not _is_video(_pkg("c.webm", "webm", "video")) and not _is_video(_pkg("p.png", "png", "image"))
    p = TwitterPoster()
    monkeypatch.setattr(p, "_creds", lambda *a, **k: ("tok", "ct0", "owner"))
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"\x00" * 200)
    assert p.validate(_pkg(clip, "mp4", "video", duration_s=42.0)) == []
    errs = p.validate(_pkg(clip, "mp4", "video", duration_s=200.0))
    assert len(errs) == 1 and "up to 140 s" in errs[0]
    monkeypatch.setattr(TWClient, "VIDEO_MAX_BYTES", 100)
    errs = p.validate(_pkg(clip, "mp4", "video", duration_s=10.0))
    assert len(errs) == 1 and "up to 512 MB" in errs[0]


def test_webm_refused_before_the_network_mp4_mov_not(tmp_path):
    from posting.manager import _get_poster
    p = _get_poster("tw")
    f = tmp_path / "c.webm"
    f.write_bytes(b"\x00")
    assert p.media_refusal(_pkg(f, "webm", "video")).startswith("X doesn't take webm video")
    for name, ft in (("c.mp4", "mp4"), ("c.mov", "mov")):
        g = tmp_path / name
        g.write_bytes(b"\x00")
        assert p.media_refusal(_pkg(g, ft, "video")) is None
