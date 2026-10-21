"""Responses are compressed (OVERVIEWSPEED): a seat's answers cross the owner's home upload before
Cloudflare compresses them, so uncompressed JSON made the Overview take seconds. Media and
already-compressed files must pass through untouched."""
from fastapi.testclient import TestClient


def _client():
    from dashboard import app
    return TestClient(app)


def test_text_is_gzipped_when_the_browser_accepts_it():
    r = _client().get("/js/app.js", headers={"Accept-Encoding": "gzip"})
    assert r.status_code == 200 and r.headers.get("content-encoding") == "gzip"


def test_images_are_not_recompressed():
    r = _client().get("/img/logo-quill.png", headers={"Accept-Encoding": "gzip"})
    assert r.status_code == 200 and "content-encoding" not in r.headers


def test_no_gzip_without_accept_encoding():
    r = _client().get("/js/app.js", headers={"Accept-Encoding": "identity"})
    assert "content-encoding" not in r.headers


def test_a_story_cover_comes_card_sized_when_asked(tmp_path, monkeypatch):
    """Library cards asked for full-size story covers (up to 170 KB each); ``w`` now gets a WebP like artwork."""
    from types import SimpleNamespace
    from PIL import Image
    from posting import story_reader
    import thumbs
    Image.new("RGB", (1600, 900), "teal").save(tmp_path / "cover.png")
    monkeypatch.setattr(story_reader, "load_story", lambda name: SimpleNamespace(path=tmp_path))
    monkeypatch.setattr(thumbs, "root", lambda: tmp_path)
    c = _client()
    small = c.get("/api/posting/image", params={"story": "Sample_Story", "file": "cover.png", "w": 400})
    full = c.get("/api/posting/image", params={"story": "Sample_Story", "file": "cover.png"})
    assert small.headers["content-type"] == "image/webp" and full.headers["content-type"] == "image/png"
