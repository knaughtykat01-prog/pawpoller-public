"""Removing a canonical tag must actually remove it (3.9.8).

`_canonical_tag_list` unions **core + default + auxiliary**. The PATCH route
wrote only `default`, so on a work using the core/auxiliary split a tag deleted
in the editor stayed in `core` and came back on the very next read.

Additions worked — they landed in `default` and joined the union — which is why
this presented as "saving works, but removals don't stick" rather than as the
save being broken.
"""
from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from posting import artwork_reader
from routes.masterpieces_api import masterpieces_router


@pytest.fixture
def art(tmp_path, monkeypatch):
    """A real artwork folder, so the round trip goes through the actual reader."""
    root = tmp_path / "artwork"
    folder = root / "Rear_View"
    folder.mkdir(parents=True)
    (folder / "image.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    monkeypatch.setattr(artwork_reader, "get_artwork_archive_path", lambda: root)
    return folder


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(masterpieces_router)
    return TestClient(app)


def _write(folder, tags: dict):
    (folder / "masterpiece.json").write_text(
        json.dumps({"title": "Rear View", "rating": "adult", "tags": tags}),
        encoding="utf-8")


def _canonical(folder):
    raw = json.loads((folder / "masterpiece.json").read_text(encoding="utf-8"))
    return artwork_reader._canonical_tag_list(raw.get("tags") or {})


def test_removing_a_core_tag_removes_it(art, client):
    """The reported bug. `solo` is in core; deleting it must not survive."""
    _write(art, {"core": ["tiger", "solo", "male"], "auxiliary": ["looking_back"]})

    r = client.patch("/api/masterpieces/Rear_View",
                     json={"tags": ["tiger", "male", "looking_back"]})
    assert r.status_code == 200

    assert "solo" not in _canonical(art)
    assert _canonical(art) == ["tiger", "male", "looking_back"]


def test_removing_an_auxiliary_tag_removes_it(art, client):
    _write(art, {"core": ["tiger"], "auxiliary": ["looking_back", "presenting"]})
    assert client.patch("/api/masterpieces/Rear_View", json={"tags": ["tiger", "looking_back"]}).status_code == 200
    assert "presenting" not in _canonical(art)


def test_the_core_split_survives_an_edit(art, client):
    """core/auxiliary is what keeps a heavily-tagged work postable to FA at all
    (500-char budget, enforced by rejection). Flattening it would break that."""
    _write(art, {"core": ["tiger", "solo"], "auxiliary": ["looking_back"]})
    assert client.patch("/api/masterpieces/Rear_View", json={"tags": ["tiger", "looking_back"]}).status_code == 200

    raw = json.loads((art / "masterpiece.json").read_text(encoding="utf-8"))
    assert raw["tags"]["core"] == ["tiger"], "a tag that was core stays core"
    assert raw["tags"]["auxiliary"] == ["looking_back"]


def test_a_new_tag_joins_the_auxiliary_tail(art, client):
    """core is a curated priority set — a new tag must not displace one."""
    _write(art, {"core": ["tiger"], "auxiliary": ["looking_back"]})
    assert client.patch("/api/masterpieces/Rear_View",
                 json={"tags": ["tiger", "looking_back", "brand_new"]}).status_code == 200

    raw = json.loads((art / "masterpiece.json").read_text(encoding="utf-8"))
    assert raw["tags"]["core"] == ["tiger"]
    assert "brand_new" in raw["tags"]["auxiliary"]


def test_the_legacy_default_key_is_dropped_on_a_split_work(art, client):
    """Left behind, it would re-union whatever it still held — the bug itself."""
    _write(art, {"core": ["tiger", "solo"], "default": ["tiger", "solo", "ghost"]})
    assert client.patch("/api/masterpieces/Rear_View", json={"tags": ["tiger"]}).status_code == 200

    raw = json.loads((art / "masterpiece.json").read_text(encoding="utf-8"))
    assert "default" not in raw["tags"]
    assert "ghost" not in _canonical(art)


def test_a_legacy_flat_work_keeps_its_shape(art, client):
    """A folder that never got the split still round-trips through `default`,
    where writing it alone genuinely is a replace."""
    _write(art, {"default": ["tiger", "solo"]})
    assert client.patch("/api/masterpieces/Rear_View", json={"tags": ["tiger"]}).status_code == 200

    raw = json.loads((art / "masterpiece.json").read_text(encoding="utf-8"))
    assert raw["tags"]["default"] == ["tiger"]
    assert _canonical(art) == ["tiger"]


def test_per_platform_overrides_are_left_alone(art, client):
    """The editor edits the canonical set. An explicit per-platform list is a
    deliberate override and is not the canonical editor's to rewrite."""
    _write(art, {"core": ["tiger", "solo"], "fa": ["fa_only_tag"]})
    assert client.patch("/api/masterpieces/Rear_View", json={"tags": ["tiger"]}).status_code == 200

    raw = json.loads((art / "masterpiece.json").read_text(encoding="utf-8"))
    assert raw["tags"]["fa"] == ["fa_only_tag"]


def test_clearing_every_tag_clears_them(art, client):
    _write(art, {"core": ["tiger"], "auxiliary": ["looking_back"]})
    assert client.patch("/api/masterpieces/Rear_View", json={"tags": []}).status_code == 200
    assert _canonical(art) == []
