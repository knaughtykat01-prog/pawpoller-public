"""Integration tests for the full posting pipeline.

Tests the complete flow: story_reader → manager → platform poster → DB,
then update flow: load publications → re-read archive → edit on platform.

All HTTP calls are mocked via respx — no real platform APIs are hit.
"""

import pytest
import respx
import httpx

import config
from database.db import init_db, get_connection
from database import posting_queries


@pytest.fixture(autouse=True)
def clean_db():
    """Ensure clean posting tables for every test."""
    init_db()
    conn = get_connection()
    conn.execute("DELETE FROM posting_log")
    conn.execute("DELETE FROM posting_queue")
    conn.execute("DELETE FROM publications")
    conn.commit()
    conn.close()


@pytest.fixture
def archive_with_stories(story_archive):
    """Configure the archive path to use the test fixture."""
    config.save_settings({"posting_story_archive_path": str(story_archive)})
    return story_archive


# ═══════════════════════════════════════════════════════════════
# INKBUNNY: Full Upload → Verify DB → Edit → Verify DB
# ═══════════════════════════════════════════════════════════════

class TestInkbunnyFullPipeline:

    @pytest.mark.asyncio
    @respx.mock
    async def test_upload_chapter_to_inkbunny(self, archive_with_stories):
        """Upload Ch1 of test story to IB, verify DB records."""
        config.save_settings({"username": "testuser", "password": "testpass"})

        # Mock IB API
        respx.post(f"{config.INKBUNNY_API_BASE}/api_login.php").mock(
            return_value=httpx.Response(200, json={"sid": "test_sid", "user_id": 1, "ratingsmask": "11111"})
        )
        respx.post(f"{config.INKBUNNY_API_BASE}/api_userrating.php").mock(
            return_value=httpx.Response(200, json={"sid": "test_sid"})
        )
        respx.post(f"{config.INKBUNNY_API_BASE}/api_upload.php").mock(
            return_value=httpx.Response(200, json={"submission_id": 55555})
        )
        respx.post(f"{config.INKBUNNY_API_BASE}/api_editsubmission.php").mock(
            return_value=httpx.Response(200, json={"submission_id": 55555})
        )

        # Run the full post pipeline
        from posting import manager
        # Pre-create the IB poster with a client that has SID set
        from posting.platforms.inkbunny import InkbunnyPoster
        from clients.ib.client import InkbunnyClient
        poster = InkbunnyPoster()
        client = InkbunnyClient(username="testuser", password="testpass")
        client.sid = "test_sid"
        poster._client = client
        manager._posters["ib"] = poster

        results = await manager.post_story("Test_Story", ["ib"], chapters=[1])

        # Verify results
        assert len(results) == 1
        r = results[0]
        assert r["success"] is True
        assert r["platform"] == "ib"
        assert r["chapter_index"] == 1
        assert r["external_id"] == "55555"
        assert "inkbunny.net" in r["external_url"]
        # `>= 0`, not `> 0` — this was a real intermittent failure. Durations come
        # from `time.monotonic()`, which on Windows is GetTickCount64() with a
        # **15.625 ms** resolution. Every HTTP call here is mocked by respx, so the
        # whole upload routinely finishes inside a single clock tick and measures
        # EXACTLY 0.0 — the faster the machine, the more often it fails. The
        # assertion's real intent is "duration is populated and sane", not "took
        # measurable wall-clock time".
        assert r["duration"] >= 0

        # Verify DB: publication created
        conn = get_connection()
        try:
            pub = posting_queries.get_publication_by_story(conn, "Test_Story", 1, "ib")
            assert pub is not None
            assert pub["external_id"] == "55555"
            assert pub["status"] == "posted"
            assert pub["title_used"] != ""
            assert pub["update_count"] == 0
            assert pub["first_posted_at"] is not None

            # Verify DB: log entry created
            logs = posting_queries.get_posting_log(conn, story_name="Test_Story")
            assert len(logs) == 1
            assert logs[0]["action"] == "post"
            assert logs[0]["status"] == "success"
            assert logs[0]["external_id"] == "55555"
            assert logs[0]["platform"] == "ib"
        finally:
            conn.close()

    @pytest.mark.asyncio
    @respx.mock
    async def test_edit_after_upload(self, archive_with_stories):
        """Upload then edit — verify update_count increments and log records both actions."""
        config.save_settings({"username": "testuser", "password": "testpass"})

        # Mock IB API
        respx.post(f"{config.INKBUNNY_API_BASE}/api_login.php").mock(
            return_value=httpx.Response(200, json={"sid": "test_sid", "user_id": 1, "ratingsmask": "11111"})
        )
        respx.post(f"{config.INKBUNNY_API_BASE}/api_userrating.php").mock(
            return_value=httpx.Response(200, json={"sid": "test_sid"})
        )
        respx.post(f"{config.INKBUNNY_API_BASE}/api_upload.php").mock(
            return_value=httpx.Response(200, json={"submission_id": 77777})
        )
        respx.post(f"{config.INKBUNNY_API_BASE}/api_editsubmission.php").mock(
            return_value=httpx.Response(200, json={"submission_id": 77777})
        )

        from posting import manager
        from posting.platforms.inkbunny import InkbunnyPoster
        from clients.ib.client import InkbunnyClient
        poster = InkbunnyPoster()
        client = InkbunnyClient(username="testuser", password="testpass")
        client.sid = "test_sid"
        poster._client = client
        manager._posters["ib"] = poster

        # Step 1: Upload
        post_results = await manager.post_story("Test_Story", ["ib"], chapters=[1])
        assert post_results[0]["success"] is True

        # Step 2: Edit/Update
        update_results = await manager.update_story("Test_Story", platforms=["ib"], chapters=[1])

        assert len(update_results) == 1
        u = update_results[0]
        assert u["success"] is True
        assert u["platform"] == "ib"
        assert u["external_id"] == "77777"

        # Verify DB: update_count incremented
        conn = get_connection()
        try:
            pub = posting_queries.get_publication_by_story(conn, "Test_Story", 1, "ib")
            assert pub["update_count"] == 1
            assert pub["last_updated_at"] is not None

            # Verify DB: two log entries (post + update)
            logs = posting_queries.get_posting_log(conn, story_name="Test_Story")
            assert len(logs) == 2
            actions = {l["action"] for l in logs}
            assert "post" in actions
            assert "update" in actions
        finally:
            conn.close()


# ═══════════════════════════════════════════════════════════════
# BLUESKY: Full Upload → Verify DB → Edit (delete+repost)
# ═══════════════════════════════════════════════════════════════

class TestBlueskyFullPipeline:

    @pytest.mark.asyncio
    @respx.mock
    async def test_upload_announcement(self, archive_with_stories):
        """Post an announcement to Bluesky, verify DB."""
        config.save_settings({
            "bsky_identifier": "test.bsky.social",
            "bsky_app_password": "test-pass",
        })

        respx.post("https://bsky.social/xrpc/com.atproto.server.createSession").mock(
            return_value=httpx.Response(200, json={
                "accessJwt": "jwt", "refreshJwt": "rjwt",
                "did": "did:plc:test", "handle": "test.bsky.social",
            })
        )
        respx.get("https://bsky.social/xrpc/com.atproto.server.getSession").mock(
            return_value=httpx.Response(200, json={"did": "did:plc:test"})
        )
        respx.post("https://bsky.social/xrpc/com.atproto.repo.createRecord").mock(
            return_value=httpx.Response(200, json={
                "uri": "at://did:plc:test/app.bsky.feed.post/abc123",
                "cid": "bafytest",
            })
        )

        from posting import manager
        results = await manager.post_story("Test_Story", ["bsky"], chapters=[0])

        assert len(results) == 1
        r = results[0]
        assert r["success"] is True
        assert "at://" in r["external_id"]
        assert "bsky.app" in r["external_url"]

        # Verify DB
        conn = get_connection()
        try:
            pub = posting_queries.get_publication_by_story(conn, "Test_Story", 0, "bsky")
            assert pub is not None
            assert pub["status"] == "posted"
            assert "at://" in pub["external_id"]
        finally:
            conn.close()

    @pytest.mark.asyncio
    @respx.mock
    async def test_edit_deletes_and_reposts(self, archive_with_stories):
        """Bluesky edit should delete old post and create new one."""
        config.save_settings({
            "bsky_identifier": "test.bsky.social",
            "bsky_app_password": "test-pass",
        })

        respx.post("https://bsky.social/xrpc/com.atproto.server.createSession").mock(
            return_value=httpx.Response(200, json={
                "accessJwt": "jwt", "refreshJwt": "rjwt",
                "did": "did:plc:test", "handle": "test.bsky.social",
            })
        )
        respx.get("https://bsky.social/xrpc/com.atproto.server.getSession").mock(
            return_value=httpx.Response(200, json={"did": "did:plc:test"})
        )

        post_count = {"n": 0}
        def _mock_create(request):
            post_count["n"] += 1
            return httpx.Response(200, json={
                "uri": f"at://did:plc:test/app.bsky.feed.post/post{post_count['n']}",
                "cid": f"bafy{post_count['n']}",
            })

        respx.post("https://bsky.social/xrpc/com.atproto.repo.createRecord").mock(side_effect=_mock_create)
        respx.post("https://bsky.social/xrpc/com.atproto.repo.deleteRecord").mock(
            return_value=httpx.Response(200, json={})
        )

        from posting import manager

        # Step 1: Post
        await manager.post_story("Test_Story", ["bsky"], chapters=[0])

        # Step 2: Update (triggers delete + repost)
        update_results = await manager.update_story("Test_Story", platforms=["bsky"], chapters=[0])

        assert len(update_results) == 1
        u = update_results[0]
        assert u["success"] is True
        # New post should have a different URI
        assert "post2" in u["external_id"]

        # Verify delete was called
        delete_calls = [c for c in respx.calls if "deleteRecord" in str(c.request.url)]
        assert len(delete_calls) == 1

        # Verify DB shows the new external_id
        conn = get_connection()
        try:
            pub = posting_queries.get_publication_by_story(conn, "Test_Story", 0, "bsky")
            assert "post2" in pub["external_id"]
            assert pub["update_count"] == 1
        finally:
            conn.close()


# ═══════════════════════════════════════════════════════════════
# MULTI-PLATFORM: Upload to IB + BSKY simultaneously
# ═══════════════════════════════════════════════════════════════

class TestMultiPlatformUpload:

    @pytest.mark.asyncio
    @respx.mock
    async def test_upload_to_two_platforms(self, archive_with_stories):
        """Upload same story to IB and BSKY, verify both recorded."""
        config.save_settings({
            "username": "testuser", "password": "testpass",
            "bsky_identifier": "test.bsky.social", "bsky_app_password": "test-pass",
        })

        # IB mocks
        respx.post(f"{config.INKBUNNY_API_BASE}/api_login.php").mock(
            return_value=httpx.Response(200, json={"sid": "sid", "user_id": 1, "ratingsmask": "11111"})
        )
        respx.post(f"{config.INKBUNNY_API_BASE}/api_userrating.php").mock(
            return_value=httpx.Response(200, json={"sid": "sid"})
        )
        respx.post(f"{config.INKBUNNY_API_BASE}/api_upload.php").mock(
            return_value=httpx.Response(200, json={"submission_id": 11111})
        )
        respx.post(f"{config.INKBUNNY_API_BASE}/api_editsubmission.php").mock(
            return_value=httpx.Response(200, json={"submission_id": 11111})
        )

        # BSKY mocks
        respx.post("https://bsky.social/xrpc/com.atproto.server.createSession").mock(
            return_value=httpx.Response(200, json={
                "accessJwt": "jwt", "refreshJwt": "rjwt",
                "did": "did:plc:test", "handle": "test.bsky.social",
            })
        )
        respx.get("https://bsky.social/xrpc/com.atproto.server.getSession").mock(
            return_value=httpx.Response(200, json={"did": "did:plc:test"})
        )
        respx.post("https://bsky.social/xrpc/com.atproto.repo.createRecord").mock(
            return_value=httpx.Response(200, json={
                "uri": "at://did:plc:test/app.bsky.feed.post/multi1",
                "cid": "bafymulti",
            })
        )

        from posting import manager
        from posting.platforms.inkbunny import InkbunnyPoster
        from clients.ib.client import InkbunnyClient
        poster = InkbunnyPoster()
        client = InkbunnyClient(username="testuser", password="testpass")
        client.sid = "sid"
        poster._client = client
        manager._posters["ib"] = poster

        results = await manager.post_story("Test_Story", ["ib", "bsky"], chapters=[1])

        # Should have 2 results (1 per platform)
        assert len(results) == 2
        ib_result = next(r for r in results if r["platform"] == "ib")
        bsky_result = next(r for r in results if r["platform"] == "bsky")

        assert ib_result["success"] is True
        assert ib_result["external_id"] == "11111"
        assert bsky_result["success"] is True
        assert "at://" in bsky_result["external_id"]

        # Verify DB has both publications
        conn = get_connection()
        try:
            pubs = posting_queries.get_publications(conn, story_name="Test_Story")
            assert len(pubs) == 2
            platforms = {p["platform"] for p in pubs}
            assert platforms == {"ib", "bsky"}
        finally:
            conn.close()


# ═══════════════════════════════════════════════════════════════
# MULTI-CHAPTER: Upload all chapters of a story
# ═══════════════════════════════════════════════════════════════

class TestMultiChapterUpload:

    @pytest.mark.asyncio
    @respx.mock
    async def test_upload_all_chapters(self, archive_with_stories):
        """Upload all chapters (auto-detected from manifest)."""
        config.save_settings({"username": "testuser", "password": "testpass"})

        respx.post(f"{config.INKBUNNY_API_BASE}/api_login.php").mock(
            return_value=httpx.Response(200, json={"sid": "sid", "user_id": 1, "ratingsmask": "11111"})
        )
        respx.post(f"{config.INKBUNNY_API_BASE}/api_userrating.php").mock(
            return_value=httpx.Response(200, json={"sid": "sid"})
        )

        upload_count = {"n": 0}
        def _mock_upload(request):
            upload_count["n"] += 1
            return httpx.Response(200, json={"submission_id": 10000 + upload_count["n"]})

        respx.post(f"{config.INKBUNNY_API_BASE}/api_upload.php").mock(side_effect=_mock_upload)
        respx.post(f"{config.INKBUNNY_API_BASE}/api_editsubmission.php").mock(
            return_value=httpx.Response(200, json={"submission_id": 0})
        )

        from posting import manager
        from posting.platforms.inkbunny import InkbunnyPoster
        from clients.ib.client import InkbunnyClient
        poster = InkbunnyPoster()
        client = InkbunnyClient(username="testuser", password="testpass")
        client.sid = "sid"
        poster._client = client
        manager._posters["ib"] = poster

        # chapters=None means auto-detect from manifest (2 chapters)
        results = await manager.post_story("Test_Story", ["ib"], chapters=None)

        assert len(results) == 2
        assert results[0]["success"] is True
        assert results[1]["success"] is True
        assert results[0]["chapter_index"] == 1
        assert results[1]["chapter_index"] == 2
        # Each chapter gets a unique submission ID
        assert results[0]["external_id"] != results[1]["external_id"]

        # Verify DB has both chapters
        conn = get_connection()
        try:
            pubs = posting_queries.get_publications(conn, story_name="Test_Story")
            assert len(pubs) == 2
            chapters = sorted(p["chapter_index"] for p in pubs)
            assert chapters == [1, 2]
        finally:
            conn.close()


# ═══════════════════════════════════════════════════════════════
# ERROR HANDLING: Platform failures don't crash the pipeline
# ═══════════════════════════════════════════════════════════════

class TestErrorHandling:

    @pytest.mark.asyncio
    @respx.mock
    async def test_upload_failure_recorded(self, archive_with_stories):
        """If IB upload fails, result shows failure and DB records it."""
        config.save_settings({"username": "testuser", "password": "testpass"})

        respx.post(f"{config.INKBUNNY_API_BASE}/api_login.php").mock(
            return_value=httpx.Response(200, json={"sid": "sid", "user_id": 1, "ratingsmask": "11111"})
        )
        respx.post(f"{config.INKBUNNY_API_BASE}/api_userrating.php").mock(
            return_value=httpx.Response(200, json={"sid": "sid"})
        )
        # Upload returns an error
        respx.post(f"{config.INKBUNNY_API_BASE}/api_upload.php").mock(
            return_value=httpx.Response(200, json={"error_code": 310, "error_message": "Invalid file type"})
        )

        from posting import manager
        from posting.platforms.inkbunny import InkbunnyPoster
        from clients.ib.client import InkbunnyClient
        poster = InkbunnyPoster()
        client = InkbunnyClient(username="testuser", password="testpass")
        client.sid = "sid"
        poster._client = client
        manager._posters["ib"] = poster

        # Set desktop mode so the auto-queue fallback doesn't kick in
        from posting import scheduler as _sched
        _sched._runtime_mode = "desktop"

        results = await manager.post_story("Test_Story", ["ib"], chapters=[1])

        assert len(results) == 1
        assert results[0]["success"] is False
        assert "Invalid file type" in results[0]["error"]

        # Verify DB: failed publication recorded
        conn = get_connection()
        try:
            pub = posting_queries.get_publication_by_story(conn, "Test_Story", 1, "ib")
            assert pub is not None
            assert pub["status"] == "failed"

            logs = posting_queries.get_posting_log(conn, story_name="Test_Story")
            assert logs[0]["status"] == "failed"
            assert "Invalid file type" in (logs[0]["error_message"] or "")
        finally:
            conn.close()

    @pytest.mark.asyncio
    @respx.mock
    async def test_update_nonexistent_story(self, archive_with_stories):
        """Updating a story that hasn't been posted returns an error."""
        from posting import manager
        results = await manager.update_story("Test_Story", platforms=["ib"])

        assert len(results) == 1
        assert "error" in results[0]
        assert "No publications" in results[0]["error"]


# ═══════════════════════════════════════════════════════════════
# VALIDATION: Bad packages rejected before API calls
# ═══════════════════════════════════════════════════════════════

class TestValidationIntegration:

    @pytest.mark.asyncio
    async def test_validation_failure_no_api_calls(self, archive_with_stories):
        """If validation fails, no HTTP calls are made."""
        config.save_settings({"username": "testuser", "password": "testpass"})

        from posting import manager, story_reader
        from posting.platforms.inkbunny import InkbunnyPoster
        from clients.ib.client import InkbunnyClient

        poster = InkbunnyPoster()
        client = InkbunnyClient(username="testuser", password="testpass")
        client.sid = "sid"
        poster._client = client
        manager._posters["ib"] = poster

        # Build a package manually with too few tags
        story = story_reader.load_story("Test_Story")
        pkg = story_reader.build_package(story, 1, "ib")
        pkg.tags = ["only", "two"]  # IB needs 4

        errors = poster.validate(pkg)
        assert len(errors) > 0
        assert any("4 tags" in e for e in errors)


# ═══════════════════════════════════════════════════════════════
# FORGET PUBLICATION — must not trip the FK back-references
# ═══════════════════════════════════════════════════════════════

class TestForgetPublication:
    """delete_publication() on a row that has already been posted.

    A posted publication is referenced by posting_log (immutable audit
    trail) and possibly posting_queue via a pub_id FK. With
    PRAGMA foreign_keys=ON a naive DELETE raised "FOREIGN KEY constraint
    failed" (500 on DELETE /api/editor/stories/{s}/publication). The fix
    unlinks the children (NULLs their pub_id) before deleting; the audit
    log must survive.
    """

    def test_delete_publication_with_log_and_queue_children(self):
        conn = get_connection()
        try:
            pub_id = posting_queries.upsert_publication(
                conn, "Chosen", 0, "ao3",
                external_id="82712456",
                external_url="https://archiveofourown.org/works/82712456",
                status="posted",
            )
            # Audit-log row (every real post writes one) referencing the pub.
            posting_queries.log_posting_action(
                conn, "ao3", "Chosen", 0, "post", "success", pub_id=pub_id,
            )
            # A queue row that also back-references the pub.
            conn.execute(
                "INSERT INTO posting_queue (story_name, chapter_index, platform, pub_id) "
                "VALUES (?, ?, ?, ?)",
                ("Chosen", 0, "ao3", pub_id),
            )
            conn.commit()

            # The bug: this raised sqlite3.IntegrityError before the fix.
            ok = posting_queries.delete_publication(conn, "Chosen", 0, "ao3")
            assert ok is True

            # Publication is gone…
            assert posting_queries.get_publication_by_story(
                conn, "Chosen", 0, "ao3") is None
            # …but the audit log survives, now unlinked (pub_id NULL).
            logs = posting_queries.get_posting_log(conn, story_name="Chosen")
            assert len(logs) == 1
            assert logs[0]["pub_id"] is None
            # …and the queue row survives, also unlinked.
            q = conn.execute(
                "SELECT pub_id FROM posting_queue WHERE story_name = ?",
                ("Chosen",),
            ).fetchone()
            assert q is not None and q["pub_id"] is None
        finally:
            conn.close()

    def test_delete_publication_absent_returns_false(self):
        conn = get_connection()
        try:
            assert posting_queries.delete_publication(
                conn, "NoSuchStory", 0, "ao3") is False
        finally:
            conn.close()
