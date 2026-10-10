"""A badly shaped upload is refused before anything is written (UPLOADTAGS500).

Found during the brand launch (2026-10-07): `POST /api/artwork/upload` with
`tags` as a plain list wrote the piece, then 500'd in `load_artwork`, and the
masterpiece.json it left behind made `GET /api/works` 500 for everyone until it
was fixed by hand. `create_artwork` now checks the shape first, so both upload
routes answer 400 and leave the archive untouched.
"""
from __future__ import annotations

import io
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from posting import artwork_reader as ar


def _png() -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (200, 90, 120)).save(buf, "PNG")
    return buf.getvalue()


@pytest.fixture
def api(tmp_path, monkeypatch):
    from routes import artwork_api
    monkeypatch.setattr(ar, "get_artwork_archive_path", lambda: tmp_path / "art")
    (tmp_path / "art").mkdir()
    app = FastAPI()
    app.include_router(artwork_api.artwork_router)
    return TestClient(app)


def _upload(api, meta):
    return api.post("/api/artwork/upload", data={"metadata": json.dumps(meta)},
                    files={"file": ("pic.png", _png(), "image/png")})


@pytest.mark.parametrize("meta, word", [
    ({"title": "Launch", "tags": ["fox", "digital"]}, "tags"),          # the live report
    ({"title": "Launch", "tags": {"core": "fox, digital"}}, "tags"),
    ({"title": "Launch", "tags": {"core": ["fox", 3]}}, "tags"),
    ({"title": "Launch", "titles": ["x"]}, "titles"),
    ({"title": "Launch", "categories": "art"}, "categories"),
    ({"title": "Launch", "platforms": "bsky"}, "platforms"),
    ({"title": ["Launch"]}, "title"),
])
def test_a_bad_shape_is_a_400_and_leaves_nothing_behind(api, tmp_path, meta, word):
    r = _upload(api, meta)
    assert r.status_code == 400, r.text
    assert word in r.json()["detail"]
    assert list((tmp_path / "art").iterdir()) == []


def test_the_shape_the_app_sends_still_works(api):
    r = _upload(api, {"title": "Launch", "rating": "general",
                      "tags": {"default": ["fox"], "bsky": ["fox", "art"]},
                      "platforms": ["bsky"], "categories": {"fa": {"cat": "1"}}})
    assert r.status_code == 200, r.text
    art = ar.load_artwork(r.json()["name"])
    assert art.tags_by_platform == {"default": ["fox"], "bsky": ["fox", "art"]}


def test_no_tags_at_all_is_fine(api):
    assert _upload(api, {"title": "Launch"}).status_code == 200
