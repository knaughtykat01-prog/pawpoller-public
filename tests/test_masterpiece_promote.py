"""Masterpieces Phase 3 (2.127.0) — promote flow + same-image suggestions.

Covers the new glue in masterpiece_queries: promote_from_submission (seed the
primary member + store the canonical pHash) and suggestions (anchored, no-AI
perceptual-hash candidates not yet members). import_artwork's network download is
stubbed — we test the mastering logic, not the fetch.
"""
import io
import json

import pytest

from database.db import get_connection
from database import masterpiece_queries as mq
from database import image_hash
from posting import artwork_reader, artwork_importer


def _make_png() -> bytes:
    """A small, structured (non-uniform) PNG so dHash yields a real hash."""
    from PIL import Image
    img = Image.new("L", (16, 16))
    img.putdata([(x * 13 + y * 7) % 256 for y in range(16) for x in range(16)])
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


_PNG = _make_png()


@pytest.fixture
def archive(tmp_path, monkeypatch):
    """Point the artwork/Masterpiece archive at a temp dir for the test."""
    root = tmp_path / "artwork"
    root.mkdir()
    monkeypatch.setattr(artwork_reader, "get_artwork_archive_path", lambda: root)
    return root


def _flip16(h):
    """A hash 16 bits away from `h` (distance 16 > HAMMING_THRESHOLD=8)."""
    return f"{int(h, 16) ^ 0xFFFF:016x}"


# ── promote_from_submission ──────────────────────────────────────

def test_promote_seeds_primary_member_and_phash(archive, monkeypatch):
    conn = get_connection()
    try:
        # Stub the importer's network path: create a real folder, return its name.
        def fake_import(platform, sid):
            name = artwork_reader.create_artwork(
                title="Imported Wolf", image_filename="i.png", image_bytes=_PNG,
                platforms=[platform], source={"platform": platform, "submission_id": str(sid)})
            return {"status": "imported", "name": name, "images": 1}
        monkeypatch.setattr(artwork_importer, "import_artwork", fake_import)
        # Source submission carries account_id 3 → the member must inherit it.
        conn.execute("INSERT INTO fa_submissions (submission_id, title, account_id) VALUES (100, 'Imported Wolf', 3)")
        conn.commit()

        res = mq.promote_from_submission(conn, "fa", "100")
        conn.commit()
        name = res["name"]

        # Primary member seeded, with the source account.
        members = mq.get_members(conn, name)
        assert len(members) == 1
        assert members[0]["platform"] == "fa" and members[0]["submission_id"] == "100"
        assert members[0]["role"] == "primary"
        assert members[0]["account_id"] == 3
        # Canonical pHash stored in image_hashes AND on masterpiece.json.
        assert image_hash.has(conn, "fa", "100")
        meta = json.loads((archive / name / "masterpiece.json").read_text(encoding="utf-8"))
        assert meta.get("phash")
    finally:
        conn.close()


def test_promote_is_idempotent(archive, monkeypatch):
    conn = get_connection()
    try:
        calls = {"n": 0}
        def fake_import(platform, sid):
            calls["n"] += 1
            # Mirror import_artwork's real idempotency: first call creates, later
            # calls find the existing folder by import_source.
            existing = artwork_importer.find_existing(platform, str(sid))
            if existing:
                return {"status": "already_imported", "name": existing, "images": 1}
            name = artwork_reader.create_artwork(
                title="Wolf", image_filename="i.png", image_bytes=_PNG,
                platforms=[platform], source={"platform": platform, "submission_id": str(sid)})
            return {"status": "imported", "name": name, "images": 1}
        monkeypatch.setattr(artwork_importer, "import_artwork", fake_import)
        conn.execute("INSERT INTO fa_submissions (submission_id, title) VALUES (100, 'Wolf')")
        conn.commit()

        a = mq.promote_from_submission(conn, "fa", "100"); conn.commit()
        b = mq.promote_from_submission(conn, "fa", "100"); conn.commit()
        assert a["name"] == b["name"]
        assert len(mq.get_members(conn, a["name"])) == 1   # not duplicated
    finally:
        conn.close()


# ── suggestions (anchored perceptual-hash) ───────────────────────

def test_suggestions_finds_same_image_non_members(archive, monkeypatch):
    conn = get_connection()
    try:
        # A masterpiece whose canonical image hashes to H.
        name = artwork_reader.create_artwork(
            title="Wolf", image_filename="i.png", image_bytes=_PNG, platforms=["fa"],
            source={"platform": "fa", "submission_id": "100"})
        img = artwork_reader.load_artwork(name)
        H = image_hash.dhash_from_path(str(img.path / img.image))
        assert H

        image_hash.ensure_table(conn)
        # Member (fa,100) and a same-image candidate (ws,110) both hash to H;
        # (ib,200) is far away (16 bits) → must be excluded.
        image_hash.store(conn, "fa", "100", H)
        image_hash.store(conn, "ws", "110", H)
        image_hash.store(conn, "ib", "200", _flip16(H))
        # Submission rows so the candidate resolves a title/thumbnail.
        conn.execute("INSERT INTO ws_submissions (submission_id, title, thumbnail_url) VALUES (110, 'Wolf on WS', 'http://cdn/w.jpg')")
        conn.execute("INSERT INTO submissions (submission_id, title) VALUES (200, 'Different')")
        conn.commit()

        mq.add_member(conn, name, "fa", "100", role="primary")
        conn.commit()

        sug = mq.suggestions(conn, name)
        keys = {(s["platform"], s["submission_id"]) for s in sug}
        assert ("ws", "110") in keys        # same image, not yet a member
        assert ("ib", "200") not in keys    # different image
        assert ("fa", "100") not in keys    # already a member
        ws = next(s for s in sug if s["platform"] == "ws")
        assert ws["similarity"] == 1.0 and ws["reason"] == "image"
        assert ws["title"] == "Wolf on WS" and ws["thumbnail_url"] == "http://cdn/w.jpg"
    finally:
        conn.close()


def test_suggestions_empty_without_seed(archive):
    conn = get_connection()
    try:
        # A masterpiece with no members and no hashes → nothing to anchor on.
        name = artwork_reader.create_artwork(
            title="Lonely", image_filename="i.png", image_bytes=_PNG, platforms=["fa"])
        image_hash.ensure_table(conn)
        # The canonical image still hashes, but there are no OTHER hashes to match.
        assert mq.suggestions(conn, name) == []
    finally:
        conn.close()
