"""Bluesky video — MEDIATYPES phase 3, site 1 (4.20.0).

Bluesky video is not an uploadBlob: the file goes to video.bsky.app with a service-auth token the
account's own PDS mints (audience did:web:<pds host>, method com.atproto.repo.uploadBlob), the
service transcodes it while a job is polled, and the resulting blob goes into an
app.bsky.embed.video on the post record. The network is faked; no media file enters the repo.
"""
from __future__ import annotations

import asyncio

import pytest

from clients.bsky.client import BskyClient, _pds_host_from
from posting.platforms.base import StoryUploadPackage
from posting.platforms.bluesky import BlueskyPoster, _is_video


class _Resp:
    def __init__(self, code, body):
        self.status_code = code
        self._body = body
        self.text = ""

    def json(self):
        return self._body

    def raise_for_status(self):
        pass


class _FakeHTTP:
    """Plays the PDS (getServiceAuth, createRecord) and video.bsky.app (limits,
    uploadVideo → job, getJobStatus running → completed)."""
    def __init__(self, *, can_upload=True, fail_job=False, conflict=False):
        self.calls = []
        self.polls = 0
        self.can_upload = can_upload
        self.fail_job = fail_job
        self.conflict = conflict

    async def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append({"m": "GET", "url": url, "params": dict(params or {}), "headers": dict(headers or {})})
        if url.endswith("getServiceAuth"):
            return _Resp(200, {"token": "svc-" + params["aud"] + "-" + params["lxm"]})
        if url.endswith("getUploadLimits"):
            return _Resp(200, {"canUpload": self.can_upload, "message": "" if self.can_upload else "Daily limit reached"})
        if url.endswith("getJobStatus"):
            self.polls += 1
            if self.polls == 1:
                return _Resp(200, {"jobStatus": {"jobId": params["jobId"], "state": "JOB_STATE_ENCODING", "progress": 50}})
            if self.fail_job:
                return _Resp(200, {"jobStatus": {"jobId": params["jobId"], "state": "JOB_STATE_FAILED", "message": "Unsupported codec"}})
            return _Resp(200, {"jobStatus": {"jobId": params["jobId"], "state": "JOB_STATE_COMPLETED",
                                             "blob": {"$type": "blob", "ref": {"$link": "bafyvideo"}, "mimeType": "video/mp4", "size": 999}}})
        raise AssertionError(url)

    async def post(self, url, json=None, content=None, headers=None, timeout=None):
        self.calls.append({"m": "POST", "url": url, "json": json, "bytes": len(content or b""), "headers": dict(headers or {}), "timeout": timeout})
        if "uploadVideo" in url:
            if self.conflict:
                return _Resp(409, {"jobId": "job-old", "state": "JOB_STATE_COMPLETED", "did": "did:plc:x",
                                   "blob": {"$type": "blob", "ref": {"$link": "bafyold"}, "mimeType": "video/mp4", "size": 1}})
            return _Resp(200, {"jobId": "job-1", "did": "did:plc:x", "state": "JOB_STATE_CREATED"})
        if url.endswith("createRecord"):
            return _Resp(200, {"uri": "at://did:plc:x/app.bsky.feed.post/abc", "cid": "bafycid"})
        raise AssertionError(url)


def _client(http):
    c = BskyClient.__new__(BskyClient)
    c._http = http
    c._access_jwt, c._refresh_jwt = "jwt", "r"
    c._did, c._handle, c._pds_host = "did:plc:x", "owner.bsky.social", "puffball.us-east.host.bsky.network"
    c._logged_in = True
    c.last_error = ""

    async def _ok():
        return True
    c.ensure_logged_in = _ok

    async def _facets(text, handles):
        return []
    c._build_facets = _facets
    return c


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(BskyClient, "VIDEO_POLL_SECONDS", 0.0)


def test_pds_host_comes_from_the_did_document():
    doc = {"service": [{"id": "#atproto_pds", "type": "AtprotoPersonalDataServer",
                        "serviceEndpoint": "https://puffball.us-east.host.bsky.network"}]}
    assert _pds_host_from(doc) == "puffball.us-east.host.bsky.network"
    assert _pds_host_from(None) == "" and _pds_host_from({"service": []}) == ""


def test_upload_video_mints_a_pds_token_uploads_and_polls_to_the_blob(tmp_path):
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"\x00" * 64)
    http = _FakeHTTP()
    c = _client(http)
    blob = asyncio.run(c.upload_video(str(clip), "video/mp4"))
    assert blob["ref"]["$link"] == "bafyvideo"
    auth = http.calls[0]
    assert auth["url"].endswith("com.atproto.server.getServiceAuth")
    assert auth["params"]["aud"] == "did:web:puffball.us-east.host.bsky.network"
    assert auth["params"]["lxm"] == "com.atproto.repo.uploadBlob" and auth["params"]["exp"] > 0
    up = http.calls[1]
    assert up["url"].startswith("https://video.bsky.app/xrpc/app.bsky.video.uploadVideo?did=did%3Aplc%3Ax&name=")
    assert up["url"].endswith(".mp4") and up["bytes"] == 64
    assert up["headers"]["Authorization"] == "Bearer svc-did:web:puffball.us-east.host.bsky.network-com.atproto.repo.uploadBlob"
    assert up["headers"]["Content-Type"] == "video/mp4" and up["timeout"] >= 600
    polls = [x for x in http.calls if x["url"].endswith("getJobStatus")]
    assert len(polls) == 2 and polls[0]["params"] == {"jobId": "job-1"}


def test_a_failed_job_reports_blueskys_words_and_a_conflict_reuses_the_blob(tmp_path):
    clip = tmp_path / "clip.mov"
    clip.write_bytes(b"\x00" * 8)
    c = _client(_FakeHTTP(fail_job=True))
    assert asyncio.run(c.upload_video(str(clip), "video/quicktime")) is None
    assert "Unsupported codec" in c.last_error
    c = _client(_FakeHTTP(conflict=True))
    assert asyncio.run(c.upload_video(str(clip), "video/quicktime"))["ref"]["$link"] == "bafyold"


def test_create_post_embeds_the_video_with_alt_and_aspect(tmp_path):
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"\x00" * 8)
    http = _FakeHTTP()
    c = _client(http)
    r = asyncio.run(c.create_post("a loop #loop", video_path=str(clip), video_alt="Sample Clip", video_aspect=(640, 360),
                                  labels=["sexual"]))
    assert r["uri"].endswith("/abc") and r["url"] == "https://bsky.app/profile/owner.bsky.social/post/abc"
    rec = [x for x in http.calls if x["url"].endswith("createRecord")][0]["json"]["record"]
    assert rec["embed"] == {"$type": "app.bsky.embed.video", "video": {"$type": "blob", "ref": {"$link": "bafyvideo"}, "mimeType": "video/mp4", "size": 999},
                            "alt": "Sample Clip", "aspectRatio": {"width": 640, "height": 360}}
    assert rec["labels"]["values"] == [{"val": "sexual"}]


def test_upload_limits_use_the_video_service_audience():
    http = _FakeHTTP(can_upload=False)
    c = _client(http)
    lim = asyncio.run(c.get_video_upload_limits())
    assert lim == {"canUpload": False, "message": "Daily limit reached"}
    assert http.calls[0]["params"]["aud"] == "did:web:video.bsky.app" and http.calls[0]["params"]["lxm"] == "app.bsky.video.getUploadLimits"
    assert http.calls[1]["headers"]["Authorization"].startswith("Bearer svc-did:web:video.bsky.app")


# ── the poster ───────────────────────────────────────────────────────────────

def _pkg(path, ft, kind, **over):
    kw = dict(story_name="Sample_Clip", chapter_index=0, chapter_title="", platform="bsky",
              title="Sample Clip", description="a loop", tags=["loop"], rating="general",
              file_path=str(path), file_type=ft, media_kind=kind, thumbnail_path="")
    kw.update(over)
    return StoryUploadPackage(**kw)


class _FakeClient:
    def __init__(self, can_upload=True):
        self.calls = []
        self.last_error = ""
        self.can_upload = can_upload

    async def get_video_upload_limits(self):
        self.calls.append(("limits",))
        return {"canUpload": self.can_upload, "message": "" if self.can_upload else "Daily limit reached"}

    async def create_post(self, text, **kw):
        self.calls.append(("post", text, kw))
        return {"uri": "at://did:plc:x/app.bsky.feed.post/abc", "cid": "c", "url": "https://bsky.app/profile/owner/post/abc"}


def _poster(fake, monkeypatch):
    p = BlueskyPoster()

    async def _ensure():
        return fake
    monkeypatch.setattr(p, "_ensure_client", _ensure)
    return p


def test_a_video_piece_is_uploaded_itself_with_its_measurements(tmp_path, monkeypatch):
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"\x00" * 8)
    poster_img = tmp_path / "poster.jpg"
    poster_img.write_bytes(b"\xff\xd8\xff")
    fake = _FakeClient()
    r = asyncio.run(_poster(fake, monkeypatch).post(_pkg(clip, "mp4", "video", thumbnail_path=str(poster_img), width=640, height=360)))
    assert r.success and r.external_id.endswith("/abc")
    assert fake.calls[0] == ("limits",)
    kw = fake.calls[1][2]
    assert kw["video_path"] == str(clip) and kw["video_aspect"] == (640, 360) and kw["video_alt"] == "Sample Clip"
    assert kw["image_path"] is None                                  # no poster embed beside a video


def test_a_used_up_allowance_is_refused_before_the_upload(tmp_path, monkeypatch):
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"\x00" * 8)
    fake = _FakeClient(can_upload=False)
    r = asyncio.run(_poster(fake, monkeypatch).post(_pkg(clip, "mp4", "video")))
    assert not r.success and "Daily limit reached" in r.error
    assert [c[0] for c in fake.calls] == ["limits"]


def test_audio_is_still_announced_with_its_poster(tmp_path, monkeypatch):
    track = tmp_path / "track.mp3"
    track.write_bytes(b"ID3")
    poster_img = tmp_path / "poster.png"
    from PIL import Image
    Image.new("RGB", (8, 8), (1, 2, 3)).save(poster_img)
    fake = _FakeClient()
    r = asyncio.run(_poster(fake, monkeypatch).post(_pkg(track, "mp3", "audio", thumbnail_path=str(poster_img))))
    assert r.success
    kw = fake.calls[0][2]
    assert kw["image_path"] and kw["video_path"] is None


def test_is_video_and_the_caps(tmp_path):
    assert _is_video(_pkg("c.mp4", "mp4", "video")) and _is_video(_pkg("c.webm", "webm", "video"))
    assert not _is_video(_pkg("c.m4v", "m4v", "video")) and not _is_video(_pkg("p.png", "png", "image"))
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"\x00" * 8)
    p = BlueskyPoster()
    assert p.validate(_pkg(clip, "mp4", "video", duration_s=42.0)) == []
    errs = p.validate(_pkg(clip, "mp4", "video", duration_s=900.0))
    assert len(errs) == 1 and "up to 10 minutes" in errs[0]


def test_m4v_refused_before_the_network_mp4_mov_webm_not(tmp_path):
    from posting.manager import _get_poster
    p = _get_poster("bsky")
    f = tmp_path / "c.m4v"
    f.write_bytes(b"\x00")
    assert p.media_refusal(_pkg(f, "m4v", "video")).startswith("Bluesky doesn't take m4v video")
    for name, ft in (("c.mp4", "mp4"), ("c.mov", "mov"), ("c.webm", "webm")):
        g = tmp_path / name
        g.write_bytes(b"\x00")
        assert p.media_refusal(_pkg(g, ft, "video")) is None
