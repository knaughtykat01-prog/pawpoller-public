"""Wrong-link auditor (2.192.0) — flags members whose platform image doesn't
match the Masterpiece's local image, by perceptual hash. Native/offline, no AI.

NB: dHash needs real structure — a solid-colour image hashes to all-zeros
(no adjacent-pixel brightness change), so every solid colour "matches". The
fixtures draw a white square in different corners so the hashes actually differ.
"""
import io
import os
import tempfile

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from database.db import get_connection
from database import masterpiece_queries as mq, image_hash


def _patterned_bytes(box):
    im = Image.new("RGB", (64, 64), (0, 0, 0))
    ImageDraw.Draw(im).rectangle(box, fill=(255, 255, 255))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def _patterned_tmp(box):
    fd, p = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    im = Image.new("RGB", (64, 64), (0, 0, 0))
    ImageDraw.Draw(im).rectangle(box, fill=(255, 255, 255))
    im.save(p, format="PNG")
    return p


_TOP_LEFT = (4, 4, 26, 26)
_BOTTOM_RIGHT = (38, 38, 60, 60)


@pytest.fixture
def client(tmp_path, monkeypatch):
    from posting import artwork_reader
    monkeypatch.setattr(artwork_reader, "get_artwork_archive_path", lambda: tmp_path)
    app = FastAPI()
    from routes.masterpieces_api import masterpieces_router
    app.include_router(masterpieces_router)
    return TestClient(app)


def _make(box):
    from posting import artwork_reader
    return artwork_reader.create_artwork(
        title="Piece", image_filename="a.png", image_bytes=_patterned_bytes(box))


def test_flags_member_whose_image_differs(client):
    from posting import artwork_reader
    name = _make(_TOP_LEFT)                              # local hero: square top-left
    local_hash = image_hash.dhash_from_path(
        str(artwork_reader.load_artwork(name).path / "a.png"))

    conn = get_connection()
    image_hash.ensure_table(conn)
    # Member A matches the local image (same hash) → NOT flagged.
    mq.add_member(conn, name, "fa", "111")
    image_hash.store(conn, "fa", "111", local_hash)
    # Member B is a clearly different image (square bottom-right) → flagged.
    mq.add_member(conn, name, "e621", "222")
    image_hash.store(conn, "e621", "222",
                     image_hash.dhash_from_path(_patterned_tmp(_BOTTOM_RIGHT)))
    # Member C has no stored hash → skipped (can't judge), not flagged.
    mq.add_member(conn, name, "ib", "333")
    conn.commit()
    conn.close()

    flagged = client.get("/api/masterpieces/mislink-audit").json()["flagged"]
    keys = {(f["platform"], f["submission_id"]) for f in flagged}
    assert ("e621", "222") in keys          # mismatched
    assert ("fa", "111") not in keys        # matching
    assert ("ib", "333") not in keys        # unjudgeable → not flagged


def test_clean_library_flags_nothing(client):
    from posting import artwork_reader
    name = _make(_TOP_LEFT)
    h = image_hash.dhash_from_path(str(artwork_reader.load_artwork(name).path / "a.png"))
    conn = get_connection()
    image_hash.ensure_table(conn)
    mq.add_member(conn, name, "fa", "1")
    image_hash.store(conn, "fa", "1", h)
    conn.commit()
    conn.close()
    assert client.get("/api/masterpieces/mislink-audit").json()["flagged"] == []
