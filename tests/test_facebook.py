"""Facebook Pages (fb) — spec 022 (4.57.0). Posting to a Page; connect by token exchange.

Every Graph call is answered by a fake transport. Names are placeholders ("Sample Page").
"""
from __future__ import annotations

import asyncio
import json
from urllib.parse import parse_qs

import httpx
import pytest
from fastapi.testclient import TestClient

import config
import clients.fb.client as fbmod
from clients.fb.client import FbClient, FbError, plain_error
from posting.platforms.base import StoryUploadPackage
from posting.platforms.facebook import FacebookPoster, build_caption, fit_photo

PAGE = {"id": "1001", "name": "Sample Page", "access_token": "PAGE-TOKEN", "tasks": ["CREATE_CONTENT", "MANAGE"]}


class FakeGraph:
    """Records calls; answers like the Graph API."""

    def __init__(self, *, error_on: str = "", error: dict | None = None, page_token_only: bool = False):
        self.calls: list[tuple[str, str, dict]] = []
        self.error_on, self.error, self.page_token_only = error_on, error, page_token_only
        self.photo_n = 0

    def __call__(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        q = dict(req.url.params)
        if req.method == "POST" and b"multipart" not in (req.headers.get("content-type", "").encode()):
            q.update({k: v[0] for k, v in parse_qs(req.content.decode()).items()})
        self.calls.append((req.method, path, q))
        if self.error_on and self.error_on in path:
            return httpx.Response(400, json={"error": self.error})
        if path.endswith("/oauth/access_token"):
            return httpx.Response(200, json={"access_token": "LONG-USER", "token_type": "bearer"})
        if path.endswith("/me/accounts"):
            if self.page_token_only:
                return httpx.Response(400, json={"error": {"code": 100, "message":
                                      "(#100) Tried accessing nonexisting field (accounts) on node type (Page)"}})
            return httpx.Response(200, json={"data": [PAGE]})
        if path.endswith("/me"):
            return httpx.Response(200, json={"id": "1001", "name": "Sample Page"})
        if path.endswith("/photos"):
            self.photo_n += 1
            return httpx.Response(200, json={"id": f"ph{self.photo_n}", "post_id": f"1001_{self.photo_n}"})
        if path.endswith("/feed"):
            return httpx.Response(200, json={"id": "1001_99"})
        if path.endswith("/videos"):
            return httpx.Response(200, json={"id": "vid7"})
        if req.method == "GET" and q.get("fields") == "permalink_url":
            return httpx.Response(200, json={"permalink_url": "https://www.facebook.com/x/posts/1"})
        if req.method == "GET" and q.get("fields") == "id,name":
            return httpx.Response(200, json={"id": "1001", "name": "Sample Page"})
        return httpx.Response(404, json={"error": {"code": 803, "message": "unknown"}})


def _client(fake, **kw):
    return FbClient(transport=httpx.MockTransport(fake), **kw)


def _png(tmp_path, name="a.png", size=(8, 8)):
    from PIL import Image
    p = tmp_path / name
    Image.new("RGB", size, (200, 120, 40)).save(p)
    return str(p)


# ── client ───────────────────────────────────────────────────────────────────

def test_exchange_then_list_pages_keeps_the_secret_out_of_the_result():
    fake = FakeGraph()

    async def go():
        async with _client(fake) as c:
            tok = await c.exchange_token("SHORT", "123", "s3cret")
            return tok, await c.list_pages(tok)
    tok, pages = asyncio.run(go())
    assert tok == "LONG-USER"
    assert pages == [{"id": "1001", "name": "Sample Page", "access_token": "PAGE-TOKEN", "can_post": True}]
    ex = fake.calls[0][2]
    assert ex["grant_type"] == "fb_exchange_token" and ex["client_secret"] == "s3cret"


def test_a_pasted_page_token_is_that_page():
    async def go():
        async with _client(FakeGraph(page_token_only=True)) as c:
            return await c.list_pages("PAGE-TOKEN")
    assert asyncio.run(go()) == [{"id": "1001", "name": "Sample Page", "access_token": "PAGE-TOKEN",
                                  "can_post": True}]


def test_one_photo_is_a_photo_post_several_are_one_feed_post(tmp_path):
    a, b = _png(tmp_path, "a.png"), _png(tmp_path, "b.png")
    fake = FakeGraph()

    async def go(paths):
        async with _client(fake, page_token="PAGE-TOKEN", page_id="1001") as c:
            return await c.post_photos(paths, "hello")
    r = asyncio.run(go([a]))
    assert r["id"] == "1001_1" and fake.calls[0][1].endswith("/1001/photos")
    fake.calls.clear()
    r = asyncio.run(go([a, b]))
    feed = [c for c in fake.calls if c[1].endswith("/feed")][0][2]
    assert r["id"] == "1001_99" and feed["message"] == "hello"
    assert json.loads(feed["attached_media[0]"])["media_fbid"] and "attached_media[1]" in feed


def test_text_and_video(tmp_path):
    vid = tmp_path / "v.mp4"
    vid.write_bytes(b"\x00" * 64)
    fake = FakeGraph()

    async def go():
        async with _client(fake, page_token="T", page_id="1001") as c:
            return await c.post_text("hi", link="https://example.com"), await c.post_video(str(vid), "T", "D")
    t, v = asyncio.run(go())
    assert t["id"] == "1001_99" and v["id"] == "vid7"
    assert any(c[1].endswith("/1001/videos") for c in fake.calls)


@pytest.mark.parametrize("err,needle", [
    ({"code": 190, "message": "Error validating access token"}, "expired"),
    ({"code": 200, "message": "(#200) requires pages_manage_posts"}, "missing a permission"),
    ({"code": 368, "message": "blocked"}, "against its rules"),
    ({"code": 4, "message": "limit"}, "Wait a few minutes"),
])
def test_graph_errors_become_plain_sentences(err, needle):
    assert needle in plain_error(400, {"error": err})


def test_a_refusal_raises_the_plain_sentence():
    fake = FakeGraph(error_on="/feed", error={"code": 190, "message": "bad"})

    async def go():
        async with _client(fake, page_token="T", page_id="1001") as c:
            await c.post_text("x")
    with pytest.raises(FbError, match="expired"):
        asyncio.run(go())


# ── poster ───────────────────────────────────────────────────────────────────

def _pkg(path, **kw):
    base = dict(story_name="Sample", chapter_index=0, chapter_title="", platform="fb", title="Sample",
                description="A sketch", tags=["fox", "fox", "digital art"], file_path=path,
                file_type=path.rsplit(".", 1)[-1], media_kind="image", rating="general")
    base.update(kw)
    return StoryUploadPackage(**base)


def test_adult_work_is_refused_before_the_network(tmp_path):
    p = FacebookPoster()
    assert "doesn't take adult work" in p.refusal(_pkg(_png(tmp_path), rating="adult"))
    assert p.refusal(_pkg(_png(tmp_path))) is None


def test_caption_has_deduped_hashtags():
    assert build_caption(_pkg("x.png")) == "A sketch\n\n#fox #digitalart"


def test_webp_becomes_jpeg_gif_passes_through(tmp_path):
    from PIL import Image
    w = tmp_path / "a.webp"
    Image.new("RGB", (8, 8)).save(w)
    out, tmp = fit_photo(str(w))
    assert out.endswith(".jpg") and tmp == out
    g = tmp_path / "a.gif"
    Image.new("P", (8, 8)).save(g)
    assert fit_photo(str(g)) == (str(g), None)


def test_poster_posts_through_the_client(tmp_path, monkeypatch):
    fake = FakeGraph()
    monkeypatch.setattr(fbmod.FbClient, "__init__",
                        lambda self, page_token="", page_id="", transport=None:
                        FbClient.__orig_init__(self, page_token, page_id, httpx.MockTransport(fake)))
    config.save_settings({"fb_page_token": "PAGE-TOKEN", "fb_page_id": "1001", "fb_page_name": "Sample Page"})
    r = asyncio.run(FacebookPoster().post(_pkg(_png(tmp_path))))
    assert r.success and r.external_id == "1001_1"


FbClient.__orig_init__ = FbClient.__init__


# ── connect API ──────────────────────────────────────────────────────────────

def test_connect_flow_lists_pages_without_tokens_then_saves_the_page(monkeypatch):
    fake = FakeGraph()
    orig = FbClient.__orig_init__
    monkeypatch.setattr(fbmod.FbClient, "__init__",
                        lambda self, page_token="", page_id="", transport=None:
                        orig(self, page_token, page_id, httpx.MockTransport(fake)))
    import dashboard
    api = TestClient(dashboard.app, raise_server_exceptions=False, client=("127.0.0.1", 50000))
    r = api.post("/api/fb/auth/pages", json={"user_token": "SHORT", "app_id": "123", "app_secret": "s3cret"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["pages"] == [{"id": "1001", "name": "Sample Page", "can_post": True}] and body["long_lived"]
    assert "PAGE-TOKEN" not in r.text
    r = api.post("/api/fb/auth/connect", json={"pick": body["pick"], "page_id": "1001"})
    assert r.status_code == 200, r.text
    s = config.get_settings()
    assert s["fb_page_token"] == "PAGE-TOKEN" and s["fb_page_id"] == "1001" and s["fb_page_name"] == "Sample Page"
    assert "s3cret" not in json.dumps(s)
    # The pick is one-time.
    assert api.post("/api/fb/auth/connect", json={"pick": body["pick"], "page_id": "1001"}).status_code == 400
    assert api.get("/api/fb/auth/status").json() == {"has_credentials": True, "username": "Sample Page"}
    assert api.post("/api/fb/auth/pages", json={"user_token": "T", "app_id": "123"}).status_code == 400


def test_facebook_is_registered_everywhere_it_must_be():
    from database import accounts
    from posting import manager, post_publisher
    from polling import account_probe
    assert "fb" in accounts.PLATFORMS and "fb" not in accounts.POST_ONLY_PLATFORMS   # polled since 4.59.0
    assert "fb" in manager.WORK_POSTERS and "fb" in post_publisher.SUPPORTED
    assert "fb" in account_probe.PROBES
    assert "fb_page_token" in config.CREDENTIAL_FIELDS


# ── live-proof fixes (2026-10-09): GIFs as video (T021), the link card (T013) ─────────────

def _gif(tmp_path, name="loop.gif"):
    from PIL import Image
    p = tmp_path / name
    frames = [Image.new("RGB", (8, 8), c) for c in ((200, 120, 40), (40, 120, 200))]
    frames[0].save(p, save_all=True, append_images=frames[1:], duration=100, loop=0)
    return str(p)


def _fake_client(monkeypatch, fake):
    monkeypatch.setattr(fbmod.FbClient, "__init__",
                        lambda self, page_token="", page_id="", transport=None:
                        FbClient.__orig_init__(self, page_token, page_id, httpx.MockTransport(fake)))


def test_a_gif_piece_goes_up_as_a_video_so_it_keeps_moving(tmp_path, monkeypatch):
    """Sent to /photos Facebook kept one still frame of the brand Page's first GIF; /videos plays it."""
    fake = FakeGraph()
    _fake_client(monkeypatch, fake)
    config.save_settings({"fb_page_token": "PAGE-TOKEN", "fb_page_id": "1001", "fb_page_name": "Sample Page"})
    pkg = _pkg(_gif(tmp_path))
    assert FacebookPoster().validate(pkg) == []
    r = asyncio.run(FacebookPoster().post(pkg))
    paths = [c[1] for c in fake.calls if c[0] == "POST"]
    assert r.success and r.external_id == "vid7"
    assert any(p.endswith("/videos") for p in paths) and not any(p.endswith("/photos") for p in paths)


def _post_settings():
    return {"fb_page_token": "PAGE-TOKEN", "fb_page_id": "1001"}


def test_a_lone_gif_on_the_posts_page_goes_up_as_a_video(tmp_path, monkeypatch):
    from posting import post_publisher
    fake = FakeGraph()
    _fake_client(monkeypatch, fake)
    post = {"body": "New loop", "rating": "general", "media": [{"path": _gif(tmp_path), "alt": ""}]}
    res = asyncio.run(post_publisher._publish_one(post, "fb", None, _post_settings()))
    paths = [c[1] for c in fake.calls if c[0] == "POST"]
    assert res["success"], res
    assert any(p.endswith("/videos") for p in paths) and not any(p.endswith("/photos") for p in paths)


def test_a_text_post_hands_facebook_its_first_link_for_the_preview_card(monkeypatch):
    from posting import post_publisher
    fake = FakeGraph()
    _fake_client(monkeypatch, fake)
    post = {"body": "We're on Facebook. Read more: https://example.com/news. Thanks!", "rating": "general"}
    res = asyncio.run(post_publisher._publish_one(post, "fb", None, _post_settings()))
    feed = [c[2] for c in fake.calls if c[0] == "POST" and c[1].endswith("/feed")]
    assert res["success"], res
    assert feed and feed[0].get("link") == "https://example.com/news" and "Read more" in feed[0]["message"]


def test_a_text_post_without_a_link_sends_none():
    from posting import post_publisher
    assert post_publisher._first_url("just words") == ""
    assert post_publisher._first_url("(see https://example.com/a)") == "https://example.com/a"
