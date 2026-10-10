"""Spec 030: Threads carries pictures, GIFs and video — against a stand-in for graph.threads.net."""
from __future__ import annotations

import asyncio
import json
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from PIL import Image

import config
from clients.thr import client as tc
from posting import ig_host, video_convert
from posting.platforms import threads as tp
from posting.platforms.base import StoryUploadPackage


class FakeThreads:
    """Containers get ids c1, c2…; `statuses` scripts each container's status checks (default FINISHED)."""

    def __init__(self):
        self.containers: dict[str, dict] = {}
        self.published: list[str] = []
        self.statuses: dict[str, list[str]] = {}
        self.error_message = ""
        self.create_error: dict | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = urlparse(str(request.url)).path
        q = parse_qs(urlparse(str(request.url)).query)
        if "refresh_access_token" in path:
            return httpx.Response(400, json={})
        if path.endswith("/me"):
            return httpx.Response(200, json={"id": "111", "username": "secondfur"})
        if request.method == "POST":
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            if path.endswith("/threads_publish"):
                self.published.append(form["creation_id"])
                return httpx.Response(200, json={"id": "m" + form["creation_id"]})
            if self.create_error:
                return httpx.Response(400, json={"error": self.create_error})
            cid = f"c{len(self.containers) + 1}"
            self.containers[cid] = form
            return httpx.Response(200, json={"id": cid})
        ident = path.rsplit("/", 1)[1]
        if "status" in (q.get("fields") or [""])[0]:
            seq = self.statuses.get(ident) or []
            status = seq.pop(0) if seq else "FINISHED"
            return httpx.Response(200, json={"status": status, "error_message": self.error_message})
        return httpx.Response(200, json={"permalink": f"https://www.threads.com/@secondfur/post/{ident}"})


@pytest.fixture
def fake(monkeypatch):
    f = FakeThreads()
    real = httpx.AsyncClient.__init__

    def init(self, *a, **kw):
        kw["transport"] = httpx.MockTransport(f)
        real(self, *a, **kw)
    monkeypatch.setattr(tc.httpx.AsyncClient, "__init__", init)
    monkeypatch.setattr(tc.ThrClient, "WAIT_FAST", 0)
    monkeypatch.setattr(tc.ThrClient, "WAIT_SLOW", 0)
    hosted_paths: list[str] = []

    sizes: list = []

    async def host(paths, settings=None):
        hosted_paths.extend(paths)
        for p in paths:                          # read now: the poster deletes its temp files after
            try:
                with Image.open(p) as im:
                    sizes.append(im.size)
            except Exception:
                sizes.append(None)
        return ig_host.Hosted([f"https://host.example/{i}" for i, _ in enumerate(paths)], "local")
    monkeypatch.setattr(ig_host, "host_images", host)
    monkeypatch.setattr(ig_host, "first_available_rung", lambda s=None: "local")
    f.hosted, f.sizes = hosted_paths, sizes
    config.save_settings({"thr_access_token": "T0", "thr_user_id": "111"})
    return f


def _img(tmp_path, name="a.png", size=(800, 600)):
    p = tmp_path / name
    Image.new("RGB", size, (200, 120, 40)).save(p)
    return p


def _gif(tmp_path, name="a.gif"):
    p = tmp_path / name
    frames = [Image.new("RGB", (101, 75), c) for c in ((255, 0, 0), (0, 0, 255))]
    frames[0].save(p, save_all=True, append_images=frames[1:], duration=100, loop=0)
    return p


def _pkg(path, *, rating="general", desc="A fox in the snow.", tags=("snow_fox", "winter"), **kw):
    ext = str(path).rsplit(".", 1)[1]
    return StoryUploadPackage(story_name="Sample Story", chapter_index=0, chapter_title="", platform="thr",
                              title="Sample Story", description=desc, tags=list(tags), rating=rating,
                              file_path=str(path), file_type=ext,
                              media_kind="video" if ext in ("mp4", "mov") else "image",
                              extra={"alt_text": "A fox"}, **kw)


def _post(pkg):
    return asyncio.run(tp.ThreadsPoster().post(pkg))


# ── Foundational: the client ────────────────────────────────────────────────

def test_a_single_picture_is_one_container_with_alt_and_topic(fake):
    r = asyncio.run(tc.ThrClient("T0", "111").create_media_post(
        "Hello", [{"kind": "image", "url": "https://h/1", "alt": "A fox"}], "snow fox"))
    assert r == {"id": "mc1", "url": "https://www.threads.com/@secondfur/post/mc1"}
    assert fake.containers["c1"] == {"media_type": "IMAGE", "image_url": "https://h/1", "alt_text": "A fox",
                                     "text": "Hello", "topic_tag": "snow fox", "access_token": "T0"}
    assert fake.published == ["c1"]


def test_several_pictures_are_one_carousel_in_order(fake):
    items = [{"kind": "image", "url": f"https://h/{i}", "alt": f"alt {i}"} for i in range(3)]
    asyncio.run(tc.ThrClient("T0", "111").create_media_post("Three", items))
    kids = [fake.containers[c] for c in ("c1", "c2", "c3")]
    assert [k["image_url"] for k in kids] == ["https://h/0", "https://h/1", "https://h/2"]
    assert all(k["is_carousel_item"] == "true" and "text" not in k for k in kids)
    assert [k["alt_text"] for k in kids] == ["alt 0", "alt 1", "alt 2"]
    assert fake.containers["c4"]["media_type"] == "CAROUSEL" and fake.containers["c4"]["children"] == "c1,c2,c3"
    assert fake.containers["c4"]["text"] == "Three" and fake.published == ["c4"]


def test_a_video_waits_until_threads_has_finished(fake):
    fake.statuses["c1"] = ["IN_PROGRESS", "IN_PROGRESS", "FINISHED"]
    asyncio.run(tc.ThrClient("T0", "111").create_media_post("", [{"kind": "video", "url": "https://h/v"}]))
    assert fake.containers["c1"]["media_type"] == "VIDEO" and fake.statuses["c1"] == [] and fake.published == ["c1"]


def test_processing_errors_permission_and_limits_are_plain_words(fake, monkeypatch):
    cli = lambda: tc.ThrClient("T0", "111")  # noqa: E731
    one = [{"kind": "video", "url": "https://h/v"}]
    fake.statuses["c1"], fake.error_message = ["ERROR"], "FAILED_DOWNLOADING_VIDEO"
    with pytest.raises(tc.ThrError, match="couldn't download the video"):
        asyncio.run(cli().create_media_post("x", one))
    assert fake.published == []
    fake.create_error = {"code": 10, "message": "Application does not have permission for this action"}
    with pytest.raises(tc.ThrError, match="threads_content_publish"):
        asyncio.run(cli().create_media_post("x", one))
    fake.create_error = {"code": 4, "message": "Application request limit reached"}
    with pytest.raises(tc.ThrError, match="try again tomorrow"):
        asyncio.run(cli().create_media_post("x", one))
    from posting.manager import _PERMANENT_ERROR_MARKERS
    assert any(m in tc.LIMIT_SENTENCE.lower() for m in _PERMANENT_ERROR_MARKERS)


def test_a_container_still_processing_after_the_limit_is_not_published(fake, monkeypatch):
    monkeypatch.setattr(tc.ThrClient, "WAIT_FAST", 1)
    monkeypatch.setattr(tc.ThrClient, "WAIT_LIMIT", 2)
    real_sleep = asyncio.sleep
    monkeypatch.setattr(tc.asyncio, "sleep", lambda s: real_sleep(0))
    fake.statuses["c1"] = ["IN_PROGRESS"] * 10
    with pytest.raises(tc.ThrError, match="still processing"):
        asyncio.run(tc.ThrClient("T0", "111").create_media_post("x", [{"kind": "video", "url": "u"}]))
    assert fake.published == []


def test_caption_fits_500_keeping_the_credit_lines():
    body = "word " * 150
    text = body.strip() + "\n\nArt by SecondFur\n\nPosted via PawPoller"
    out, short = tp.fit_caption(text)
    assert short and len(out) <= 500
    assert out.endswith("…\n\nArt by SecondFur\n\nPosted via PawPoller")
    assert tp.fit_caption("short")[1] is False


def test_topic_is_the_first_usable_tag():
    assert tp.topic_of(["snow_fox", "x"]) == "snow fox"
    assert tp.topic_of(["...", "a.b&c"]) == "abc"
    assert len(tp.topic_of(["x" * 80])) == 50 and tp.topic_of([]) == ""


def test_a_gif_becomes_an_even_sized_mp4(tmp_path):
    out = asyncio.run(video_convert.gif_to_mp4(str(_gif(tmp_path))))
    try:
        data = open(out, "rb").read()
        assert data[4:8] == b"ftyp" and len(data) > 500
    finally:
        import os
        os.remove(out)


# ── US1: the Posts page ─────────────────────────────────────────────────────

def test_posts_page_pictures_reach_threads_as_a_carousel(fake, tmp_path):
    from posting import post_publisher
    a, b = _img(tmp_path, "one.png"), _img(tmp_path, "two.png")
    post = {"body": "Two pictures", "rating": "general",
            "media": [{"path": str(a), "alt": "one"}, {"path": str(b), "alt": "two"}]}
    res = asyncio.run(post_publisher._publish_one(post, "thr", None, config.get_settings()))
    assert res["success"], res
    assert [fake.containers[c]["alt_text"] for c in ("c1", "c2")] == ["one", "two"]
    assert fake.containers["c3"]["media_type"] == "CAROUSEL"
    assert "thr" not in post_publisher.rules()["text_only"]
    assert "Text only" not in json.dumps(post_publisher.preview("x", ["thr"], image_count=2))


def test_posts_page_text_still_goes_as_text(fake):
    from posting import post_publisher
    res = asyncio.run(post_publisher._publish_one({"body": "Words only"}, "thr", None, config.get_settings()))
    assert res["success"], res
    assert fake.containers["c1"] == {"media_type": "TEXT", "text": "Words only", "access_token": "T0"}


# ── US2: a Library piece ────────────────────────────────────────────────────

def test_a_picture_piece_is_published_with_caption_alt_and_topic(fake, tmp_path):
    r = _post(_pkg(_img(tmp_path)))
    assert r.success and r.external_id == "mc1" and r.error is None
    c = fake.containers["c1"]
    assert (c["media_type"], c["alt_text"], c["text"], c["topic_tag"]) == ("IMAGE", "A fox", "A fox in the snow.",
                                                                           "snow fox")


def test_a_video_piece_posts_as_video(fake, tmp_path):
    v = tmp_path / "clip.mp4"
    v.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"0" * 100)
    r = _post(_pkg(v, duration_s=30))
    assert r.success and fake.containers["c1"]["media_type"] == "VIDEO"


def test_a_long_caption_is_shortened_and_says_so(fake, tmp_path):
    r = _post(_pkg(_img(tmp_path), desc="word " * 200))
    assert r.success and r.error == tp.SHORTENED and len(fake.containers["c1"]["text"]) <= 500


def test_a_tiny_picture_is_enlarged_to_threads_minimum(fake, tmp_path):
    _post(_pkg(_img(tmp_path, size=(200, 100))))
    assert fake.sizes == [(320, 160)]


def test_validate_refuses_what_threads_cant_take(fake, tmp_path):
    p = tp.ThreadsPoster()
    assert any("10 times" in e for e in p.validate(_pkg(_img(tmp_path, size=(2200, 200)))))
    v = tmp_path / "long.mp4"
    v.write_bytes(b"0" * 10)
    assert any("5 minutes" in e for e in p.validate(_pkg(v, duration_s=400)))
    assert p.validate(_pkg(_img(tmp_path, "ok.png"))) == []


def test_threads_is_a_publish_target_everywhere_it_must_be():
    from posting import manager, tag_budget
    assert manager._POSTER_CLASSES["thr"] == ("posting.platforms.threads", "ThreadsPoster")
    assert tag_budget.budget_for("thr")["count"] == 1
    js = open("frontend/js/artwork.js", encoding="utf-8").read()
    assert "'tum', 'thr']" in js


# ── US3: GIFs keep moving ───────────────────────────────────────────────────

def test_a_gif_piece_goes_up_as_a_video(fake, tmp_path):
    r = _post(_pkg(_gif(tmp_path)))
    assert r.success, r.error
    assert fake.containers["c1"]["media_type"] == "VIDEO" and fake.hosted[0].endswith(".mp4")


def test_a_gif_among_posts_pictures_is_a_video_item_in_its_place(fake, tmp_path):
    from posting import post_publisher
    post = {"body": "Mixed", "media": [{"path": str(_img(tmp_path)), "alt": "still"},
                                       {"path": str(_gif(tmp_path)), "alt": "moving"}]}
    res = asyncio.run(post_publisher._publish_one(post, "thr", None, config.get_settings()))
    assert res["success"], res
    assert [fake.containers[c]["media_type"] for c in ("c1", "c2")] == ["IMAGE", "VIDEO"]


def test_without_the_encoder_a_gif_is_refused_before_upload(fake, tmp_path, monkeypatch):
    monkeypatch.setattr(video_convert, "_ffmpeg", lambda: "")
    assert video_convert.NO_ENCODER in tp.ThreadsPoster().validate(_pkg(_gif(tmp_path)))
    r = _post(_pkg(_gif(tmp_path, "b.gif")))
    assert not r.success and "can't turn GIFs into video" in r.error and fake.containers == {}


# ── US4: mature and adult kept off ──────────────────────────────────────────

def test_mature_is_refused_before_anything_is_sent(fake, tmp_path):
    from posting import post_publisher
    pkg = _pkg(_img(tmp_path), rating="mature")
    assert "doesn't take mature work" in tp.ThreadsPoster().refusal(pkg)
    res = asyncio.run(post_publisher._publish_one({"body": "x", "rating": "mature"}, "thr", None,
                                                  config.get_settings()))
    assert not res["success"] and "mature or adult" in res["error"] and fake.containers == {}
    prev = post_publisher.preview("x", ["thr"], rating="mature")
    assert any(w["level"] == "block" and "mature or adult" in w["text"] for w in prev["thr"]["warnings"])
