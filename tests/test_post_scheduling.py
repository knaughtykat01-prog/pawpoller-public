"""Scheduling microblog posts through the shared posting_queue (SCHEDULING Phase 2).

Posts reuse the story/artwork queue with content_type='post' and story_name=the
post_id, so the existing scheduler daemon fires them and the Queue & Schedule
page / reschedule / cancel all work for free. These tests cover the two new
seams: the scheduler's post branch (dispatch to post_publisher) and the schedule
endpoint (one queue row per platform, with a readable snippet).
"""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from database import posting_queries, posts_queries
from database.db import get_connection


def _future_iso(days=1):
    return (datetime.now(timezone.utc) + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")


@pytest.mark.asyncio
async def test_scheduler_dispatches_post_row_to_publisher(db_conn, monkeypatch):
    """A content_type='post' queue row calls post_publisher.publish_post with the
    post_id parsed back out of story_name, then marks the row completed."""
    from posting import scheduler, post_publisher

    post_id = posts_queries.create_post(db_conn, body="hello world", now="2026-01-01 00:00:00")
    posting_queries.add_to_queue(
        db_conn, str(post_id), 0, "bsky", action="post",
        content_type="post", title_override="hello world")

    seen = {}

    async def fake_publish(pid, platforms, account_ids=None, settings=None):
        seen["call"] = (pid, list(platforms), account_ids)
        return [{"success": True, "external_id": "at://x", "external_url": "", "account_id": 0}]

    monkeypatch.setattr(post_publisher, "publish_post", fake_publish)

    item = posting_queries.get_queue(db_conn, content_type="post")[0]
    await scheduler._process_queue_item(item)

    assert seen["call"][0] == post_id       # str(post_id) round-trips to the int
    assert seen["call"][1] == ["bsky"]      # this one platform only

    conn = get_connection()
    try:
        row = conn.execute("SELECT status FROM posting_queue WHERE queue_id=?",
                            (item["queue_id"],)).fetchone()
    finally:
        conn.close()
    assert row["status"] == "completed"


@pytest.mark.asyncio
async def test_scheduler_non_numeric_post_id_fails_gracefully(db_conn, monkeypatch):
    """A malformed post row (non-numeric story_name) is marked failed, not crashed."""
    from posting import scheduler, post_publisher

    async def fake_publish(*a, **k):  # must never be reached
        raise AssertionError("publish_post should not run for a malformed row")

    monkeypatch.setattr(post_publisher, "publish_post", fake_publish)

    posting_queries.add_to_queue(
        db_conn, "not-a-number", 0, "bsky", action="post",
        content_type="post", title_override="oops")
    item = posting_queries.get_queue(db_conn, content_type="post")[0]
    await scheduler._process_queue_item(item)     # must not raise

    conn = get_connection()
    try:
        row = conn.execute("SELECT status FROM posting_queue WHERE queue_id=?",
                            (item["queue_id"],)).fetchone()
    finally:
        conn.close()
    assert row["status"] == "failed"


@pytest.mark.asyncio
async def test_schedule_post_endpoint_queues_one_row_per_platform(db_conn):
    from routes.posts_api import schedule_post

    long_body = "A scheduled hello to the whole wide world, several times over. " * 3
    post_id = posts_queries.create_post(db_conn, body=long_body, now="2026-01-01 00:00:00")
    future = _future_iso()

    resp = await schedule_post(post_id, {"platforms": ["bsky", "mast"], "scheduled_at": future})
    assert resp["ok"] is True
    assert len(resp["queue_ids"]) == 2

    rows = posting_queries.get_queue(db_conn, content_type="post")
    assert len(rows) == 2
    assert {r["platform"] for r in rows} == {"bsky", "mast"}
    assert all(r["story_name"] == str(post_id) for r in rows)
    assert all(r["content_type"] == "post" for r in rows)
    assert all(r["scheduled_at"] == resp["scheduled_at"] for r in rows)
    # Snippet stashed for the Queue & Schedule label — present and capped at 60.
    assert all(r["title_override"] and len(r["title_override"]) <= 60 for r in rows)


@pytest.mark.asyncio
async def test_schedule_post_unknown_id_404(db_conn):
    from routes.posts_api import schedule_post
    with pytest.raises(HTTPException) as exc:
        await schedule_post(999999, {"platforms": ["bsky"], "scheduled_at": _future_iso()})
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_schedule_post_requires_platform_and_time(db_conn):
    from routes.posts_api import schedule_post
    post_id = posts_queries.create_post(db_conn, body="hi", now="2026-01-01 00:00:00")

    with pytest.raises(HTTPException) as exc:
        await schedule_post(post_id, {"platforms": [], "scheduled_at": _future_iso()})
    assert exc.value.status_code == 400

    with pytest.raises(HTTPException) as exc:
        await schedule_post(post_id, {"platforms": ["bsky"]})
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_schedule_post_rejects_past_time(db_conn):
    from routes.posts_api import schedule_post
    post_id = posts_queries.create_post(db_conn, body="hi", now="2026-01-01 00:00:00")
    past = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S")
    with pytest.raises(HTTPException) as exc:
        await schedule_post(post_id, {"platforms": ["bsky"], "scheduled_at": past})
    assert exc.value.status_code == 400


def test_scheduled_items_query_includes_posts(db_conn):
    """The global agenda query surfaces post rows alongside stories/artwork."""
    posting_queries.add_to_queue(
        db_conn, "7", 0, "bsky", action="post",
        content_type="post", scheduled_at="2099-01-01 08:00:00", title_override="a post")
    posting_queries.add_to_queue(
        db_conn, "A_Story", 1, "ib", action="post",
        content_type="story", scheduled_at="2099-01-02 08:00:00")

    items = posting_queries.get_scheduled_items(db_conn)
    types = {i["content_type"] for i in items}
    assert "post" in types and "story" in types
