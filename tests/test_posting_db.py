"""Tests for posting database schema and queries."""

import pytest
from database import posting_queries


class TestPostingSchema:
    """Verify the posting tables are created and queryable."""

    def test_tables_exist(self, db_conn):
        tables = {r[0] for r in db_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        assert "publications" in tables
        assert "posting_queue" in tables
        assert "posting_log" in tables

    def test_publications_columns(self, db_conn):
        cols = {r[1] for r in db_conn.execute("PRAGMA table_info(publications)").fetchall()}
        assert "pub_id" in cols
        assert "story_name" in cols
        assert "platform" in cols
        assert "external_id" in cols
        assert "status" in cols
        assert "first_posted_at" in cols
        assert "update_count" in cols

    def test_posting_queue_columns(self, db_conn):
        cols = {r[1] for r in db_conn.execute("PRAGMA table_info(posting_queue)").fetchall()}
        assert "queue_id" in cols
        assert "action" in cols
        assert "scheduled_at" in cols
        assert "attempts" in cols
        assert "max_attempts" in cols

    def test_posting_log_columns(self, db_conn):
        cols = {r[1] for r in db_conn.execute("PRAGMA table_info(posting_log)").fetchall()}
        assert "log_id" in cols
        assert "action" in cols
        assert "duration_seconds" in cols


class TestPublicationsCRUD:

    def test_upsert_creates_new(self, db_conn):
        pub_id = posting_queries.upsert_publication(
            db_conn, "Test_Story", 1, "ib",
            external_id="12345",
            external_url="https://inkbunny.net/s/12345",
            title_used="Test Chapter 1",
            tags_used=["furry", "test"],
            status="posted",
        )
        assert pub_id > 0

        pub = posting_queries.get_publication(db_conn, pub_id)
        assert pub is not None
        assert pub["story_name"] == "Test_Story"
        assert pub["chapter_index"] == 1
        assert pub["platform"] == "ib"
        assert pub["external_id"] == "12345"
        assert pub["status"] == "posted"
        assert pub["update_count"] == 0

    def test_upsert_updates_existing(self, db_conn):
        pub_id1 = posting_queries.upsert_publication(
            db_conn, "Test_Story", 1, "ib",
            external_id="12345", status="posted",
        )
        pub_id2 = posting_queries.upsert_publication(
            db_conn, "Test_Story", 1, "ib",
            external_id="12345",
            title_used="Updated Title",
            status="posted",
        )
        # Same pub_id due to UNIQUE constraint
        assert pub_id1 == pub_id2

        pub = posting_queries.get_publication(db_conn, pub_id1)
        assert pub["title_used"] == "Updated Title"
        assert pub["update_count"] == 1

    def test_get_publications_filter_by_story(self, db_conn):
        posting_queries.upsert_publication(db_conn, "Story_A", 0, "ib", status="posted")
        posting_queries.upsert_publication(db_conn, "Story_B", 0, "ib", status="posted")

        pubs = posting_queries.get_publications(db_conn, story_name="Story_A")
        assert len(pubs) == 1
        assert pubs[0]["story_name"] == "Story_A"

    def test_get_publications_filter_by_platform(self, db_conn):
        posting_queries.upsert_publication(db_conn, "Story_A", 0, "ib", status="posted")
        posting_queries.upsert_publication(db_conn, "Story_A", 0, "sf", status="posted")

        pubs = posting_queries.get_publications(db_conn, platform="sf")
        assert len(pubs) == 1
        assert pubs[0]["platform"] == "sf"

    def test_get_publication_by_story(self, db_conn):
        posting_queries.upsert_publication(db_conn, "Story_X", 2, "bsky", external_id="at://test")
        pub = posting_queries.get_publication_by_story(db_conn, "Story_X", 2, "bsky")
        assert pub is not None
        assert pub["external_id"] == "at://test"

    def test_get_publication_by_story_not_found(self, db_conn):
        pub = posting_queries.get_publication_by_story(db_conn, "Nonexistent", 0, "ib")
        assert pub is None


class TestPostingQueue:

    def test_add_and_get(self, db_conn):
        qid = posting_queries.add_to_queue(
            db_conn, "Test_Story", 1, "ib", "post",
        )
        assert qid > 0

        items = posting_queries.get_pending_queue(db_conn)
        assert len(items) == 1
        assert items[0]["story_name"] == "Test_Story"
        assert items[0]["status"] == "pending"
        assert items[0]["attempts"] == 0

    def test_queue_priority_ordering(self, db_conn):
        posting_queries.add_to_queue(db_conn, "Low", 0, "ib", "post", priority=0)
        posting_queries.add_to_queue(db_conn, "High", 0, "ib", "post", priority=10)
        posting_queries.add_to_queue(db_conn, "Medium", 0, "ib", "post", priority=5)

        items = posting_queries.get_pending_queue(db_conn)
        assert items[0]["story_name"] == "High"
        assert items[1]["story_name"] == "Medium"
        assert items[2]["story_name"] == "Low"

    def test_update_status_processing(self, db_conn):
        qid = posting_queries.add_to_queue(db_conn, "S", 0, "ib", "post")
        posting_queries.update_queue_status(db_conn, qid, "processing")

        row = db_conn.execute("SELECT * FROM posting_queue WHERE queue_id = ?", (qid,)).fetchone()
        assert row["status"] == "processing"
        assert row["attempts"] == 1
        assert row["started_at"] is not None

    # ── Claiming (the double-post race) ────────────────────────
    # The desktop and the server both start the posting scheduler. Sharing the
    # queue between them is only safe if exactly one can move a row out of
    # 'pending'. See docs/specs/desktop_server_mirroring.md §0.2.

    def test_claim_succeeds_once_and_only_once(self, db_conn):
        qid = posting_queries.add_to_queue(db_conn, "S", 0, "ib", "post")
        assert posting_queries.claim_queue_item(db_conn, qid, "server:box-a") is True
        # A second scheduler reaching the same row must lose.
        assert posting_queries.claim_queue_item(db_conn, qid, "desktop:box-b") is False

        row = db_conn.execute("SELECT * FROM posting_queue WHERE queue_id = ?", (qid,)).fetchone()
        assert row["status"] == "processing"
        assert row["attempts"] == 1          # NOT 2 — the loser did no work
        assert row["claimed_by"] == "server:box-a"
        assert row["started_at"] is not None

    def test_claim_refuses_a_cancelled_row(self, db_conn):
        qid = posting_queries.add_to_queue(db_conn, "S", 0, "ib", "post")
        assert posting_queries.cancel_queue_item(db_conn, qid)
        assert posting_queries.claim_queue_item(db_conn, qid) is False
        row = db_conn.execute("SELECT status FROM posting_queue WHERE queue_id = ?", (qid,)).fetchone()
        assert row["status"] == "cancelled"

    def test_claim_is_reacquirable_after_a_retry_resets_to_pending(self, db_conn):
        qid = posting_queries.add_to_queue(db_conn, "S", 0, "ib", "post")
        assert posting_queries.claim_queue_item(db_conn, qid, "server:box-a") is True
        posting_queries.update_queue_status(db_conn, qid, "pending", error="boom")
        assert posting_queries.claim_queue_item(db_conn, qid, "server:box-a") is True
        row = db_conn.execute("SELECT attempts FROM posting_queue WHERE queue_id = ?", (qid,)).fetchone()
        assert row["attempts"] == 2          # the retry is a real second attempt

    @pytest.mark.asyncio
    async def test_scheduler_skips_an_item_it_cannot_claim(self, db_conn, monkeypatch):
        """A lost claim must abort the item, not post it anyway."""
        from posting import scheduler, manager

        qid = posting_queries.add_to_queue(db_conn, "S", 0, "ib", "post")
        item = dict(posting_queries.get_queue(db_conn)[0])
        # Another instance claims it between the SELECT and the claim.
        assert posting_queries.claim_queue_item(db_conn, qid, "other") is True

        async def must_not_run(*a, **k):
            raise AssertionError("a lost claim must not reach the poster")

        monkeypatch.setattr(manager, "post_story", must_not_run)
        await scheduler._process_queue_item(item)

        row = db_conn.execute(
            "SELECT status, claimed_by FROM posting_queue WHERE queue_id = ?", (qid,)).fetchone()
        assert row["claimed_by"] == "other"      # untouched by the loser
        assert row["status"] == "processing"

    def test_update_status_completed(self, db_conn):
        # Create a publication first so FK is valid
        pub_id = posting_queries.upsert_publication(db_conn, "S", 0, "ib", status="posted")
        qid = posting_queries.add_to_queue(db_conn, "S", 0, "ib", "post")
        posting_queries.update_queue_status(db_conn, qid, "completed", pub_id=pub_id)

        row = db_conn.execute("SELECT * FROM posting_queue WHERE queue_id = ?", (qid,)).fetchone()
        assert row["status"] == "completed"
        assert row["pub_id"] == pub_id
        assert row["completed_at"] is not None

    def test_update_status_failed(self, db_conn):
        qid = posting_queries.add_to_queue(db_conn, "S", 0, "ib", "post")
        posting_queries.update_queue_status(db_conn, qid, "failed", error="Connection timeout")

        row = db_conn.execute("SELECT * FROM posting_queue WHERE queue_id = ?", (qid,)).fetchone()
        assert row["status"] == "failed"
        assert row["last_error"] == "Connection timeout"

    def test_cancel_pending(self, db_conn):
        qid = posting_queries.add_to_queue(db_conn, "S", 0, "ib", "post")
        assert posting_queries.cancel_queue_item(db_conn, qid) is True

        row = db_conn.execute("SELECT status FROM posting_queue WHERE queue_id = ?", (qid,)).fetchone()
        assert row["status"] == "cancelled"

    def test_cancel_non_pending_fails(self, db_conn):
        qid = posting_queries.add_to_queue(db_conn, "S", 0, "ib", "post")
        posting_queries.update_queue_status(db_conn, qid, "completed")
        assert posting_queries.cancel_queue_item(db_conn, qid) is False

    def test_pending_queue_excludes_scheduled_future(self, db_conn):
        posting_queries.add_to_queue(
            db_conn, "Future", 0, "ib", "post",
            scheduled_at="2099-12-31 23:59:59",
        )
        posting_queries.add_to_queue(db_conn, "Now", 0, "ib", "post")

        items = posting_queries.get_pending_queue(db_conn)
        assert len(items) == 1
        assert items[0]["story_name"] == "Now"


class TestScheduling:
    """Reschedule + the global scheduled-items agenda query (Phase 1)."""

    def test_reschedule_moves_pending(self, db_conn):
        qid = posting_queries.add_to_queue(
            db_conn, "S", 0, "ib", "post", scheduled_at="2099-01-01 08:00:00")
        assert posting_queries.reschedule_queue_item(db_conn, qid, "2099-06-15 20:00:00") is True

        row = db_conn.execute(
            "SELECT scheduled_at FROM posting_queue WHERE queue_id = ?", (qid,)).fetchone()
        assert row["scheduled_at"] == "2099-06-15 20:00:00"

    def test_reschedule_refuses_non_pending(self, db_conn):
        qid = posting_queries.add_to_queue(
            db_conn, "S", 0, "ib", "post", scheduled_at="2099-01-01 08:00:00")
        posting_queries.update_queue_status(db_conn, qid, "completed")
        # A completed row must not be movable — the work is already done.
        assert posting_queries.reschedule_queue_item(db_conn, qid, "2099-06-15 20:00:00") is False
        row = db_conn.execute(
            "SELECT scheduled_at FROM posting_queue WHERE queue_id = ?", (qid,)).fetchone()
        assert row["scheduled_at"] == "2099-01-01 08:00:00"

    def test_reschedule_unknown_id(self, db_conn):
        assert posting_queries.reschedule_queue_item(db_conn, 999999, "2099-06-15 20:00:00") is False

    def test_get_scheduled_items_only_future_pending(self, db_conn):
        posting_queries.add_to_queue(
            db_conn, "Later", 0, "ib", "post", scheduled_at="2099-12-31 23:59:59")
        posting_queries.add_to_queue(
            db_conn, "Sooner", 0, "sf", "post", scheduled_at="2099-01-01 08:00:00")
        # Immediate (no scheduled_at) — queued, not scheduled; excluded.
        posting_queries.add_to_queue(db_conn, "Immediate", 0, "ib", "post")
        # Cancelled scheduled row — excluded.
        qid = posting_queries.add_to_queue(
            db_conn, "Gone", 0, "ib", "post", scheduled_at="2099-03-03 03:03:03")
        posting_queries.cancel_queue_item(db_conn, qid)

        items = posting_queries.get_scheduled_items(db_conn)
        names = [i["story_name"] for i in items]
        assert names == ["Sooner", "Later"]  # soonest first

    def test_get_scheduled_items_spans_content_types(self, db_conn):
        posting_queries.add_to_queue(
            db_conn, "A_Story", 1, "ib", "post",
            content_type="story", scheduled_at="2099-01-01 08:00:00")
        posting_queries.add_to_queue(
            db_conn, "An_Artwork", 0, "fa", "post",
            content_type="artwork", scheduled_at="2099-01-02 08:00:00")

        items = posting_queries.get_scheduled_items(db_conn)
        types = {i["content_type"] for i in items}
        assert types == {"story", "artwork"}


class TestPostingLog:

    def test_log_action(self, db_conn):
        log_id = posting_queries.log_posting_action(
            db_conn, "ib", "Test_Story", 1,
            action="post", status="success",
            external_id="12345",
            duration_seconds=3.5,
        )
        assert log_id > 0

        logs = posting_queries.get_posting_log(db_conn)
        assert len(logs) == 1
        assert logs[0]["platform"] == "ib"
        assert logs[0]["action"] == "post"
        assert logs[0]["status"] == "success"
        assert logs[0]["duration_seconds"] == 3.5

    def test_log_filter_by_story(self, db_conn):
        posting_queries.log_posting_action(db_conn, "ib", "A", 0, "post", "success")
        posting_queries.log_posting_action(db_conn, "ib", "B", 0, "post", "success")

        logs = posting_queries.get_posting_log(db_conn, story_name="A")
        assert len(logs) == 1
        assert logs[0]["story_name"] == "A"
