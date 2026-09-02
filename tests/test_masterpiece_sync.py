"""Masterpieces Phase 5 (2.129.0) — canonical edit (PATCH) + Sync-all.

update_artwork must push canonical metadata (metadata-only) to members whose
poster supports editing, and SKIP non-editable platforms as post-only. The PATCH
route edits masterpiece.json's canonical fields while preserving real per-platform
tag overrides. Posters are stubbed — no real platform HTTP.
"""
import pytest

from database.db import get_connection
from database import masterpiece_queries as mq
from posting import artwork_reader, manager
from posting.platforms.base import PostResult
from routes import masterpieces_api as api
from fastapi import HTTPException


@pytest.fixture
def archive(tmp_path, monkeypatch):
    root = tmp_path / "artwork"
    root.mkdir()
    monkeypatch.setattr(artwork_reader, "get_artwork_archive_path", lambda: root)
    return root


class _EditStub:
    """Editable poster — records the edits it's asked to make."""
    supports_edit = True
    requires_mode = "any"

    def __init__(self):
        self.edited = []

    def validate(self, package):
        return []

    async def edit(self, external_id, package):
        self.edited.append((external_id, package))
        return PostResult(success=True, external_id=external_id,
                          external_url=f"https://x/{external_id}", duration_seconds=0.01)

    async def _rate_limit(self):
        pass


class _PostOnlyStub(_EditStub):
    supports_edit = False


class _LiteratureOnlyStub(_EditStub):
    """DeviantArt's shape: edits stories fine, has no image-deviation endpoint."""
    supports_artwork_edit = False

    async def edit(self, external_id, package):      # pragma: no cover
        raise AssertionError(
            "update_artwork must not call edit() on a poster that cannot edit artwork"
        )


# ── Sync-all (update_artwork) ────────────────────────────────────

@pytest.mark.asyncio
async def test_update_artwork_syncs_editable_skips_postonly(archive, monkeypatch):
    name = artwork_reader.create_artwork(
        title="Wolf", image_filename="i.png", image_bytes=b"\x89PNG fake",
        description="Canonical desc", rating="mature",
        tags={"default": ["a", "b", "c", "d"]})
    conn = get_connection()
    try:
        mq.add_member(conn, name, "ib", "100", role="primary")     # editable
        mq.add_member(conn, name, "bsky", "200")                   # post-only
        conn.commit()
    finally:
        conn.close()

    ib_stub = _EditStub()
    monkeypatch.setattr(manager, "_get_poster",
                        lambda platform, account_id=None: ib_stub if platform == "ib" else _PostOnlyStub())

    results = await manager.update_artwork(name)

    ib_r = next(r for r in results if r["platform"] == "ib")
    bsky_r = next(r for r in results if r["platform"] == "bsky")
    assert ib_r["success"] is True
    assert bsky_r.get("skipped") is True and bsky_r["reason"] == "post-only"

    # The editable member got a METADATA-ONLY edit with the canonical fields.
    assert len(ib_stub.edited) == 1
    ext_id, pkg = ib_stub.edited[0]
    assert ext_id == "100"
    assert pkg.extra.get("skip_content_refresh") is True
    assert pkg.title == "Wolf" and pkg.rating == "mature"

    # A content_type='artwork' publication now records the synced metadata.
    conn = get_connection()
    try:
        from database import posting_queries
        pub = posting_queries.get_publication_by_story(conn, name, 0, "ib", content_type="artwork")
        assert pub and pub["status"] == "posted"
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_update_artwork_skips_a_poster_that_cannot_edit_artwork(archive, monkeypatch):
    """DeviantArt reports supports_edit=True — true of literature, false of images
    (its API has deviation/literature/update and no image equivalent).

    Attempting it anyway crashed on prod 2026-09-02 reading a JPEG as UTF-8, and
    the crash was the lesser harm: update_artwork recorded status='failed'
    against a LIVE, correctly-posted deviation. Skipping is what keeps the row
    honest, so this asserts the row is left completely alone.
    """
    from database import posting_queries

    name = artwork_reader.create_artwork(
        title="Fox", image_filename="i.png", image_bytes=b"\x89PNG fake",
        description="Canonical desc", rating="mature",
        tags={"default": ["a", "b", "c", "d"]})
    conn = get_connection()
    try:
        mq.add_member(conn, name, "da", "1375013761", role="primary")
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(manager, "_get_poster",
                        lambda platform, account_id=None: _LiteratureOnlyStub())

    results = await manager.update_artwork(name)

    da_r = next(r for r in results if r["platform"] == "da")
    assert da_r.get("skipped") is True
    assert da_r["reason"] == "post-only"
    assert da_r["success"] is False

    # And crucially: no publication row was written at all, so nothing claims a
    # live deviation failed.
    conn = get_connection()
    try:
        pub = posting_queries.get_publication_by_story(conn, name, 0, "da",
                                                       content_type="artwork")
        assert pub is None, "a skipped member must not write a publication row"
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_update_artwork_no_members(archive, monkeypatch):
    name = artwork_reader.create_artwork(
        title="Lonely", image_filename="i.png", image_bytes=b"x", tags={"default": ["a"]})
    monkeypatch.setattr(manager, "_get_poster", lambda platform, account_id=None: _EditStub())
    results = await manager.update_artwork(name)
    assert results and "error" in results[0]


# ── Canonical edit (PATCH route) ─────────────────────────────────

def test_patch_updates_canonical_and_preserves_platform_tags(archive):
    name = artwork_reader.create_artwork(
        title="Old", image_filename="i.png", image_bytes=b"x",
        tags={"default": ["old"], "fa": ["fa_special"]})

    out = api.update_masterpiece(name, {
        "title": "New Title", "description": "New desc", "rating": "adult",
        "characters": ["Tester"], "tags": ["wolf", "canine"]})
    assert out["status"] == "updated"

    raw = artwork_reader.read_raw_metadata(name)
    assert raw["title"] == "New Title"
    assert raw["rating"] == "adult"
    assert raw["characters"] == ["Tester"]
    assert raw["tags"]["default"] == ["wolf", "canine"]     # canonical updated
    assert raw["tags"]["fa"] == ["fa_special"]              # per-platform override preserved


def test_patch_rejects_bad_rating(archive):
    name = artwork_reader.create_artwork(
        title="X", image_filename="i.png", image_bytes=b"x", tags={"default": ["a"]})
    with pytest.raises(HTTPException) as ei:
        api.update_masterpiece(name, {"rating": "explicit"})   # not general|mature|adult
    assert ei.value.status_code == 400


def test_patch_no_fields_is_400(archive):
    name = artwork_reader.create_artwork(
        title="X", image_filename="i.png", image_bytes=b"x", tags={"default": ["a"]})
    with pytest.raises(HTTPException) as ei:
        api.update_masterpiece(name, {})
    assert ei.value.status_code == 400
