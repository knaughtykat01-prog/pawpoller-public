"""Replace a Masterpiece's canonical image (2.153.0).

The point of the feature is that swapping in a better/higher-res file must NOT
cost you the record: canonical metadata and every site-link survive, the old file
stays as a gallery alternate, and the stale perceptual hash is dropped so the
de-dup finder re-reads the new pixels.
"""
import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image

import config
from database.db import get_connection
from database import masterpiece_queries as mq, image_hash


def _png(size=(24, 24), colour=(200, 30, 30)):
    buf = io.BytesIO()
    Image.new("RGB", size, colour).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def client(tmp_path, monkeypatch):
    # Point the artwork archive at a temp dir so nothing real is touched.
    monkeypatch.setattr(config, "_settings", {**config.get_settings(),
                                              "artwork_archive_path": str(tmp_path)}, raising=False)
    from posting import artwork_reader
    monkeypatch.setattr(artwork_reader, "get_artwork_archive_path", lambda: tmp_path)
    from fastapi import FastAPI
    from routes.masterpieces_api import masterpieces_router
    app = FastAPI()
    app.include_router(masterpieces_router)
    return TestClient(app)


def _make_art(name="Piece", title="A Piece"):
    from posting import artwork_reader
    return artwork_reader.create_artwork(
        title=title, image_filename="orig.png", image_bytes=_png(),
        description="keep me", rating="adult", tags={"default": ["fox", "ref"]})


def test_replace_keeps_metadata_members_and_old_file(client):
    name = _make_art()
    conn = get_connection()
    mq.add_member(conn, name, "fa", "111")
    mq.add_member(conn, name, "bsky", "222")
    conn.commit()
    # A stale hero hash that must be invalidated by the replace.
    image_hash.ensure_table(conn)
    image_hash.store(conn, "__mp__", name, "ffffffffffffffff")
    conn.commit()
    conn.close()

    r = client.post(f"/api/masterpieces/{name}/image",
                    files={"file": ("better.png", _png((64, 64), (10, 90, 200)), "image/png")})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "replaced"
    assert body["image"] == "better.png"
    assert body["previous"] == "orig.png"

    from posting import artwork_reader
    art = artwork_reader.load_artwork(name)
    # Hero now points at the new file...
    assert art.image == "better.png"
    # ...and the canonical record is untouched.
    assert art.title == "A Piece"
    assert art.description == "keep me"
    assert art.rating == "adult"
    assert art.tags_by_platform.get("default") == ["fox", "ref"]
    # Old file survives as a gallery alternate (non-destructive).
    assert "orig.png" in body["images"] and "better.png" in body["images"]

    conn = get_connection()
    # Site-links survive → pooled stats/links carry over.
    assert sorted(mq.member_pairs(conn, name)) == [("bsky", "222"), ("fa", "111")]
    # Stale hero hash dropped so the de-dup finder re-reads the NEW pixels.
    assert conn.execute(
        "SELECT COUNT(*) FROM image_hashes WHERE platform='__mp__' AND submission_id=?",
        (name,)).fetchone()[0] == 0
    conn.close()


def test_replace_never_clobbers_an_existing_filename(client):
    name = _make_art(name="Piece2", title="Two")
    # Re-upload under the SAME name as the current hero — must not overwrite it.
    r = client.post(f"/api/masterpieces/{name}/image",
                    files={"file": ("orig.png", _png((40, 40), (5, 200, 90)), "image/png")})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["image"] == "orig_v1.png"          # de-duplicated filename
    assert "orig.png" in body["images"]            # the original still exists


def test_replace_rejects_non_image_and_missing(client):
    name = _make_art(name="Piece3", title="Three")
    r = client.post(f"/api/masterpieces/{name}/image",
                    files={"file": ("evil.exe", b"MZ", "application/octet-stream")})
    assert r.status_code == 415
    r = client.post("/api/masterpieces/NoSuchPiece/image",
                    files={"file": ("x.png", _png(), "image/png")})
    assert r.status_code == 404
