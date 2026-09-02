"""Artwork surfaces expose variants (2.190.0).

The gallery grid shows a tile per variant and the detail page a variant strip,
so both the list and detail endpoints must carry the declared variants (the
artwork detail previously returned only the single master `image`).
"""
import json
from pathlib import Path

import pytest

from posting import artwork_reader


@pytest.fixture
def artwork_archive(tmp_path, monkeypatch):
    arch = tmp_path / "Artwork"
    arch.mkdir()
    monkeypatch.setattr(artwork_reader, "get_artwork_archive_path", lambda: arch)
    return arch


def _piece_with_variant(arch):
    name = artwork_reader.create_artwork(
        title="Ki Ref", image_filename="a.png", image_bytes=b"hero", rating="adult")
    d = arch / name
    (d / "image_2.png").write_bytes(b"nsfw")
    artwork_reader.save_artwork_metadata(name, {"variants": [
        {"key": "", "label": "SFW", "image": "a.png", "rating": ""},
        {"key": "nsfw", "label": "NSFW", "image": "image_2.png", "rating": "adult"},
    ]})
    return name


def test_list_artworks_carries_variants(artwork_archive):
    name = _piece_with_variant(artwork_archive)
    row = next(a for a in artwork_reader.list_artworks() if a["name"] == name)
    keys = {v["key"] for v in row["variants"]}
    assert keys == {"", "nsfw"}


def test_list_artworks_plain_piece_has_empty_variants(artwork_archive):
    artwork_reader.create_artwork(title="Plain", image_filename="p.png", image_bytes=b"x")
    row = next(a for a in artwork_reader.list_artworks() if a["name"].startswith("Plain"))
    assert row["variants"] == []


def test_works_api_carries_artwork_variants(artwork_archive, monkeypatch):
    """The Library grid (bookshelf) reads /api/works — assemble_works must pass
    each artwork's non-primary variants (with a ready thumb_url), or the Library
    shows only the master (the 2.190.0 miss: tiles were built for the retired
    Artwork hub instead)."""
    from fastapi.testclient import TestClient
    import dashboard
    monkeypatch.setattr(dashboard.config, "is_dashboard_auth_required", lambda: False)
    c = TestClient(dashboard.app)

    name = _piece_with_variant(artwork_archive)
    works = c.get("/api/works", params={"type": "artwork"}).json()["works"]
    w = next(x for x in works if x["name"] == name)
    # Primary is the master card; only the non-primary variant is a tile.
    assert [v["label"] for v in w["variants"]] == ["NSFW"]
    assert w["variants"][0]["thumb_url"].startswith("/api/artwork/image?")


def test_detail_endpoint_returns_variants_and_images(artwork_archive, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routes.artwork_api import artwork_router
    # get_artwork_detail imports artwork_reader at module scope; it already sees
    # the patched archive via the fixture.
    app = FastAPI()
    app.include_router(artwork_router)
    c = TestClient(app)

    name = _piece_with_variant(artwork_archive)
    d = c.get(f"/api/artwork/images/{name}").json()
    labels = {v["label"] for v in d["variants"]}
    assert labels == {"SFW", "NSFW"}
    # `images` lists every image file in the folder (strip fallback source).
    assert set(d["images"]) == {"a.png", "image_2.png"}
