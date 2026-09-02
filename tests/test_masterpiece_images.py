"""Masterpiece detail gallery (2.152.0): GET /{name} lists every folder image.

Multi-image tweet sets and preserved SFW/NSFW variants live beside the hero as
image_N.*; the detail endpoint returns them all, hero first, so the frontend
can render a gallery strip.
"""
import pytest
from fastapi.testclient import TestClient

from posting import artwork_reader


@pytest.fixture
def artwork_archive(tmp_path, monkeypatch):
    arch = tmp_path / "Artwork"
    arch.mkdir()
    monkeypatch.setattr(artwork_reader, "get_artwork_archive_path", lambda: arch)
    return arch


def _mk_masterpiece(arch, name, images):
    d = arch / name
    d.mkdir()
    (d / "masterpiece.json").write_text(
        '{"title": "T", "rating": "general", "image": "%s"}' % images[0], encoding="utf-8")
    for img in images:
        (d / img).write_bytes(b"\x89PNG\r\n\x1a\nfake")
    return d


def test_detail_lists_all_images_hero_first(artwork_archive):
    _mk_masterpiece(artwork_archive, "SetPiece", ["image.png", "image_2.png", "image_3.png"])
    from dashboard import app
    r = TestClient(app).get("/api/masterpieces/SetPiece")
    assert r.status_code == 200
    data = r.json()
    assert data["images"][0] == data["image"]          # hero leads
    assert data["images"] == ["image.png", "image_2.png", "image_3.png"]


def test_detail_single_image_still_lists_one(artwork_archive):
    _mk_masterpiece(artwork_archive, "Solo", ["image.jpg"])
    from dashboard import app
    r = TestClient(app).get("/api/masterpieces/Solo")
    assert r.status_code == 200
    assert r.json()["images"] == ["image.jpg"]
