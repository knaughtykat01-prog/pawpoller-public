"""Separating an undeclared alt, and booru top-lists showing the real title (3.36.0).

Two problems from the same root — a **multi-image import** attaches every image
from one source post to a single Masterpiece:

1. Those extra images are bare files with no `variants` entry. The detail page
   rendered them as "Alt 1 / Alt 2" chips, but the Manage-variants panel only
   appears when declared variants exist and `/variants/{key}/split` needs a
   variant to name — so several unrelated artworks could share one record with
   **no way at all** to pull them apart short of editing masterpiece.json.
2. e621 and Furbooru posts carry **no title field** (verified against the live
   API), so their clients synthesise one from the first line of the description.
   Every "Top scored / Top favourited" row therefore showed a slice of prose that
   matched nothing in the Library.
"""
from __future__ import annotations

import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

import config
from database.db import get_connection
from database import masterpiece_queries as mq


def _png(colour=(200, 30, 30)):
    buf = io.BytesIO()
    Image.new("RGB", (24, 24), colour).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "_settings", {**config.get_settings(),
                                              "artwork_archive_path": str(tmp_path)}, raising=False)
    from posting import artwork_reader
    monkeypatch.setattr(artwork_reader, "get_artwork_archive_path", lambda: tmp_path)
    from fastapi import FastAPI
    from routes.masterpieces_api import masterpieces_router
    app = FastAPI()
    app.include_router(masterpieces_router)
    return TestClient(app)


def _import_with_alts(title="A Piece", alts=2):
    """The shape a multi-image import leaves: one record, several bare images."""
    from posting import artwork_reader
    name = artwork_reader.create_artwork(
        title=title, image_filename="image.png", image_bytes=_png(),
        description="from one source post", rating="adult",
        tags={"default": ["fox"]}, characters=["Ki"])
    art = artwork_reader.load_artwork(name)
    for i in range(2, 2 + alts):
        (Path(art.path) / f"image_{i}.png").write_bytes(_png((10, 40 * i, 200)))
    return name


# ── separating an undeclared alt ─────────────────────────────────────────────

def test_an_undeclared_alt_separates_into_its_own_masterpiece(client):
    name = _import_with_alts()
    r = client.post(f"/api/masterpieces/{name}/images/split",
                    json={"image": "image_2.png", "new_name": "Its Own Thing"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "split"

    from posting import artwork_reader
    new = artwork_reader.load_artwork(body["new_name"])
    assert new.title == "Its Own Thing"
    # The image moved: gone from the parent, present in the new record.
    parent = artwork_reader.load_artwork(name)
    assert not (Path(parent.path) / "image_2.png").is_file()
    assert (Path(new.path) / new.image).is_file()


def test_the_parent_keeps_every_site_link(client):
    """The whole point: a bare alt has never been posted from this record, so its
    uploads do not exist and the parent's must not move."""
    name = _import_with_alts()
    conn = get_connection()
    try:
        for plat, sid in (("e621", "111"), ("fa", "222"), ("ib", "333")):
            mq.add_member(conn, name, plat, sid, role="crosspost")
        conn.commit()
    finally:
        conn.close()

    r = client.post(f"/api/masterpieces/{name}/images/split", json={"image": "image_2.png"})
    assert r.status_code == 200, r.text
    assert r.json()["members_moved"] == 0

    conn = get_connection()
    try:
        assert len(mq.get_members(conn, name)) == 3
        assert mq.get_members(conn, r.json()["new_name"]) == []
    finally:
        conn.close()


def test_the_primary_image_cannot_be_separated(client):
    name = _import_with_alts()
    r = client.post(f"/api/masterpieces/{name}/images/split", json={"image": "image.png"})
    assert r.status_code == 422
    assert "primary" in r.json()["detail"]


def test_a_missing_or_escaping_path_is_refused(client):
    name = _import_with_alts()
    for bad in ("nope.png", "../secrets.png", "", "masterpiece.json"):
        r = client.post(f"/api/masterpieces/{name}/images/split", json={"image": bad})
        assert r.status_code == 422, f"{bad!r} -> {r.status_code}"


def test_an_already_declared_variant_is_sent_to_the_normal_path(client):
    name = _import_with_alts()
    assert client.post(f"/api/masterpieces/{name}/variants",
                       json={"key": "sfw", "image": "image_2.png", "label": "SFW"}
                       ).status_code == 200
    r = client.post(f"/api/masterpieces/{name}/images/split", json={"image": "image_2.png"})
    assert r.status_code == 409
    assert "already a declared variant" in r.json()["detail"]


def test_separating_leaves_the_remaining_alt_separable(client):
    """Two alts: taking one must not strand the other in a half-declared state."""
    name = _import_with_alts(alts=2)
    assert client.post(f"/api/masterpieces/{name}/images/split",
                       json={"image": "image_2.png"}).status_code == 200
    r = client.post(f"/api/masterpieces/{name}/images/split", json={"image": "image_3.png"})
    assert r.status_code == 200, r.text

    from posting import artwork_reader
    raw = artwork_reader.read_raw_metadata(name) or {}
    assert not (raw.get("variants") or []), "a lone leftover primary is meaningless"


def test_the_new_record_is_hashed_so_the_link_finder_sees_it(client):
    name = _import_with_alts()
    r = client.post(f"/api/masterpieces/{name}/images/split", json={"image": "image_2.png"})
    new_name = r.json()["new_name"]
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT 1 FROM image_hashes WHERE platform='__mp__' AND submission_id=?",
            (new_name,)).fetchone()
        assert row, "without a hash, 'Link the same image elsewhere' can never find its uploads"
    finally:
        conn.close()


# ── booru top-lists show the canonical title ─────────────────────────────────

def test_canonical_titles_replace_the_description_slice(client):
    name = _import_with_alts(title="Poolside Squeeze")
    conn = get_connection()
    try:
        mq.add_member(conn, name, "e621", "6660945", role="crosspost")
        conn.commit()

        rows = [{"submission_id": "6660945", "title": "Dug out of the archives — the swim"}]
        mq.apply_canonical_titles(conn, "e621", rows)
        assert rows[0]["title"] == "Poolside Squeeze"
    finally:
        conn.close()


def test_an_unlinked_post_keeps_the_title_it_had():
    """Never blank a row — an unlinked booru post still has to read as something."""
    conn = get_connection()
    try:
        rows = [{"submission_id": "no-such-id", "title": "some description slice"}]
        mq.apply_canonical_titles(conn, "e621", rows)
        assert rows[0]["title"] == "some description slice"
    finally:
        conn.close()


def test_the_lookup_is_scoped_to_one_platform(client):
    """A submission id is only unique within its platform; a cross-platform
    collision would relabel the wrong post."""
    name = _import_with_alts(title="Right Piece")
    conn = get_connection()
    try:
        mq.add_member(conn, name, "fa", "999", role="crosspost")
        conn.commit()
        assert mq.canonical_titles(conn, "e621", ["999"]) == {}
        assert mq.canonical_titles(conn, "fa", ["999"]) == {"999": "Right Piece"}
    finally:
        conn.close()


def test_an_empty_id_list_does_no_work():
    conn = get_connection()
    try:
        assert mq.canonical_titles(conn, "e621", []) == {}
        assert mq.canonical_titles(conn, "e621", ["", "  "]) == {}
    finally:
        conn.close()
