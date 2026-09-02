"""Integration test for the artwork posting pipeline.

artwork_reader → manager.post_artwork → (stub poster) → DB. Verifies the
package is built as an image, the registry records content_type='artwork',
and the Stories views stay isolated. The poster is stubbed (monkeypatching
manager._get_poster) so no real platform HTTP is hit.
"""

import pytest

from database.db import init_db, get_connection
from database import posting_queries
from posting import artwork_reader, manager
from posting.platforms.base import PostResult


class _StubPoster:
    """Records the package it was asked to post and reports success."""
    platform_id = "ib"
    requires_mode = "any"

    def __init__(self):
        self.posted = []

    def validate(self, package):
        return []

    async def post(self, package):
        self.posted.append(package)
        return PostResult(success=True, external_id="999",
                          external_url="https://example/999", duration_seconds=0.01)

    async def _rate_limit(self):
        pass


@pytest.fixture
def artwork_archive(tmp_path, monkeypatch):
    arch = tmp_path / "Artwork"
    arch.mkdir()
    monkeypatch.setattr(artwork_reader, "get_artwork_archive_path", lambda: arch)
    return arch


@pytest.fixture(autouse=True)
def clean_db():
    init_db()
    conn = get_connection()
    conn.execute("DELETE FROM posting_log")
    conn.execute("DELETE FROM posting_queue")
    conn.execute("DELETE FROM publications")
    conn.commit()
    conn.close()


@pytest.mark.asyncio
async def test_post_artwork_pipeline(artwork_archive, monkeypatch):
    name = artwork_reader.create_artwork(
        title="Pipeline Art", image_filename="pic.png", image_bytes=b"\x89PNG fake",
        rating="mature",
        tags={"default": ["a", "b", "c", "d", "e"]},
    )
    stub = _StubPoster()
    monkeypatch.setattr(manager, "_get_poster", lambda platform, account_id=None: stub)

    results = await manager.post_artwork(name, ["ib"])

    # Result + stub received an IMAGE package (chapter 0, image file_type).
    assert len(results) == 1 and results[0]["success"] is True
    assert results[0]["external_id"] == "999"
    assert len(stub.posted) == 1
    pkg = stub.posted[0]
    assert pkg.file_type == "png"
    assert pkg.file_path.endswith("pic.png")
    assert pkg.chapter_index == 0
    assert pkg.tags == ["a", "b", "c", "d", "e"]

    # Registry: a content_type='artwork' publication, isolated from Stories.
    conn = get_connection()
    try:
        art = posting_queries.get_publication_by_story(
            conn, name, 0, "ib", content_type="artwork")
        assert art is not None
        assert art["status"] == "posted"
        assert art["content_type"] == "artwork"
        # Stories view (default content_type='story') doesn't see it.
        story_pubs = posting_queries.get_publications(conn)
        assert all(p["content_type"] == "story" for p in story_pubs)
        assert name not in {p["story_name"] for p in story_pubs}
        # An artwork log entry was recorded.
        log = posting_queries.get_posting_log(conn, content_type="artwork")
        assert any(e["platform"] == "ib" and e["status"] == "success" for e in log)
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_post_artwork_auto_links_masterpiece_member(artwork_archive, monkeypatch):
    """Publishing IS mastering (Phase 4): a successful artwork post auto-adds a
    masterpiece_member (linked_via='publication') for the folder, so a fresh
    Masterpiece accumulates its members as it's posted. A failed post does not."""
    from database import masterpiece_queries as mq

    name = artwork_reader.create_artwork(
        title="Linked Art", image_filename="pic.png", image_bytes=b"\x89PNG fake",
        tags={"default": ["a", "b", "c", "d"]})
    stub = _StubPoster()
    monkeypatch.setattr(manager, "_get_poster", lambda platform, account_id=None: stub)

    await manager.post_artwork(name, ["ib"])

    conn = get_connection()
    try:
        members = mq.get_members(conn, name)
        assert len(members) == 1
        m = members[0]
        assert (m["platform"], m["submission_id"]) == ("ib", "999")   # stub external_id
        assert m["linked_via"] == "publication" and m["role"] == "crosspost"
        # The folder was adopted into the masterpieces index too.
        assert conn.execute("SELECT 1 FROM masterpieces WHERE name = ?", (name,)).fetchone()
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_failed_post_does_not_link_member(artwork_archive, monkeypatch):
    """A failed/validation-rejected post leaves no masterpiece_member."""
    from database import masterpiece_queries as mq

    name = artwork_reader.create_artwork(
        title="Unlinked", image_filename="x.png", image_bytes=b"x", tags={"default": ["one"]})

    class _Rejecting(_StubPoster):
        def validate(self, package):
            return ["needs more tags"]

    monkeypatch.setattr(manager, "_get_poster", lambda platform, account_id=None: _Rejecting())
    await manager.post_artwork(name, ["ib"])

    conn = get_connection()
    try:
        assert mq.get_members(conn, name) == []
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_post_artwork_validation_failure(artwork_archive, monkeypatch):
    """A poster reporting validation errors yields a failed result and no post."""
    name = artwork_reader.create_artwork(
        title="Bad", image_filename="x.png", image_bytes=b"x", tags={"default": ["one"]})

    class _Rejecting(_StubPoster):
        def validate(self, package):
            return ["needs more tags"]

    rej = _Rejecting()
    monkeypatch.setattr(manager, "_get_poster", lambda platform, account_id=None: rej)

    results = await manager.post_artwork(name, ["ib"])
    assert results[0]["success"] is False
    assert "needs more tags" in results[0]["error"]
    assert len(rej.posted) == 0
