"""Unit tests for Instagram (ig) posting — the container→publish flow + guards.

No network: the HTTP helpers are monkeypatched. Covers the single-image and
carousel flows, the require-image guard, and the pending-image token validation
(path-traversal guard on the public-hosting endpoint).
"""

import asyncio

import pytest

from clients.ig.client import IgClient
from posting import ig_media


def test_create_post_requires_image():
    c = IgClient(access_token="t", user_id="42")
    c._logged_in = True
    with pytest.raises(RuntimeError):
        asyncio.run(c.create_post("a caption", []))


def test_create_post_single_image_flow():
    c = IgClient(access_token="t", user_id="42")
    c._logged_in = True
    calls = {"containers": 0, "publishes": 0}

    async def fake_post(url, data):
        if url.endswith("/media"):
            calls["containers"] += 1
            assert data.get("image_url") == "https://x/img.jpg"
            assert data.get("caption") == "hello"
            return {"id": "CONTAINER123"}
        if url.endswith("/media_publish"):
            calls["publishes"] += 1
            assert data["creation_id"] == "CONTAINER123"
            return {"id": "MEDIA999"}
        return {}

    async def fake_get(url, params=None):
        if url.endswith("CONTAINER123"):
            return {"status_code": "FINISHED"}
        if url.endswith("MEDIA999"):
            return {"permalink": "https://www.instagram.com/p/xyz/"}
        return {}

    c._post_json = fake_post
    c._get_json = fake_get
    r = asyncio.run(c.create_post("hello", ["https://x/img.jpg"]))
    assert r["id"] == "MEDIA999"
    assert r["url"] == "https://www.instagram.com/p/xyz/"
    assert calls["containers"] == 1 and calls["publishes"] == 1


def test_create_post_carousel_flow():
    c = IgClient(access_token="t", user_id="42")
    c._logged_in = True
    made = []

    async def fake_post(url, data):
        if url.endswith("/media"):
            if data.get("media_type") == "CAROUSEL":
                made.append("carousel")
                assert data.get("children")   # child ids joined
                return {"id": "CARO"}
            made.append("child")
            assert data.get("is_carousel_item") == "true"
            return {"id": f"CH{made.count('child')}"}
        if url.endswith("/media_publish"):
            assert data["creation_id"] == "CARO"
            return {"id": "MEDIA_CARO"}
        return {}

    async def fake_get(url, params=None):
        return {"status_code": "FINISHED"}

    c._post_json = fake_post
    c._get_json = fake_get
    r = asyncio.run(c.create_post("cap", ["u1", "u2"]))
    assert r["id"] == "MEDIA_CARO"
    assert made.count("child") == 2 and made.count("carousel") == 1


def test_create_post_raises_on_container_error():
    c = IgClient(access_token="t", user_id="42")
    c._logged_in = True

    async def fake_post(url, data):
        return {"id": "C1"}

    async def fake_get(url, params=None):
        return {"status_code": "ERROR"}   # Meta couldn't process the image

    c._post_json = fake_post
    c._get_json = fake_get
    with pytest.raises(RuntimeError):
        asyncio.run(c.create_post("cap", ["https://x/bad.jpg"]))


def test_ig_media_path_for_rejects_bad_tokens():
    # Path-traversal + malformed tokens never resolve to a file.
    assert ig_media.path_for("../etc/passwd") is None
    assert ig_media.path_for("not-a-token") is None
    assert ig_media.path_for("g" * 32) is None          # 32 chars but non-hex
    assert ig_media.path_for("abc") is None
    # A well-formed but non-existent token is also None (no file on disk).
    assert ig_media.path_for("a" * 32) is None


def test_ig_media_public_url():
    url = ig_media.public_url("https://pawpoller.syncopates.app/", "a" * 32)
    assert url == "https://pawpoller.syncopates.app/api/ig/pubmedia/" + ("a" * 32) + ".jpg"


def test_ig_media_stash_bytes_roundtrips():
    # Raw image bytes (as the relay endpoint receives them) stash to a resolvable
    # JPEG token — even when the source is a PNG (Instagram is JPEG-only).
    import io as _io
    from PIL import Image
    buf = _io.BytesIO()
    Image.new("RGB", (20, 20), "blue").save(buf, format="PNG")
    token = ig_media.stash_bytes(buf.getvalue())
    try:
        assert ig_media.path_for(token) is not None
    finally:
        ig_media.cleanup(token)


class _FakeResp:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


class _FakeAsyncClient:
    """Minimal httpx.AsyncClient stand-in: records the call, returns a set resp."""
    resp = _FakeResp()
    captured: dict = {}

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, files=None, headers=None):
        _FakeAsyncClient.captured = {"url": url, "files": files, "headers": headers}
        return _FakeAsyncClient.resp


def test_relay_stash_image_uploads_and_returns_url(monkeypatch, tmp_path):
    import httpx
    from PIL import Image
    from posting import post_publisher

    p = tmp_path / "img.png"
    Image.new("RGB", (10, 10), "red").save(p)

    _FakeAsyncClient.resp = _FakeResp(
        200, {"token": "b" * 32, "url": "https://srv/api/ig/pubmedia/" + ("b" * 32) + ".jpg"}
    )
    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)

    out = asyncio.run(post_publisher._relay_stash_image("https://srv/", "pp_key", str(p)))
    assert out.endswith(".jpg")
    cap = _FakeAsyncClient.captured
    assert cap["url"] == "https://srv/api/ig/pubmedia"        # trailing slash trimmed
    assert cap["headers"]["Authorization"] == "Bearer pp_key"  # reuses the pairing key
    assert "file" in cap["files"]                              # multipart upload


def test_relay_stash_image_raises_on_error(monkeypatch, tmp_path):
    import httpx
    from PIL import Image
    from posting import post_publisher

    p = tmp_path / "img.png"
    Image.new("RGB", (10, 10), "red").save(p)

    _FakeAsyncClient.resp = _FakeResp(500, {}, "boom")
    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)

    with pytest.raises(RuntimeError):
        asyncio.run(post_publisher._relay_stash_image("https://srv", "k", str(p)))
