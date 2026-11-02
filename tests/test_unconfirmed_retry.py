"""UNCONFRETRY: an upload the site may have taken is never put back in the queue.

A poster says *unconfirmed* when it sent the work but couldn't find it afterwards. The work may be
live, so posting it again risks a duplicate. The scheduler used to set such a row back to
``pending`` (when the manager queued no retry and attempts were left), and the next pass posted it
again.
"""
from __future__ import annotations

import asyncio

from database import posting_queries
from database.db import get_connection


def _row(qid):
    conn = get_connection()
    try:
        return conn.execute("SELECT * FROM posting_queue WHERE queue_id = ?", (qid,)).fetchone()
    finally:
        conn.close()


def _run(monkeypatch, result):
    from posting import manager, scheduler

    async def fake_post_story(name, platforms, chapters, **kw):
        return [result]

    monkeypatch.setattr(manager, "post_story", fake_post_story)
    conn = get_connection()
    try:
        qid = posting_queries.add_to_queue(conn, "Sample_Story", 1, "ib")
    finally:
        conn.close()
    item = _row(qid)
    assert item["attempts"] < item["max_attempts"]
    asyncio.run(scheduler._process_queue_item_core(item))
    return _row(qid)


def test_unconfirmed_never_back_to_pending(monkeypatch):
    row = _run(monkeypatch, {"platform": "ib", "success": False, "unconfirmed": True,
                             "retry_queued": False, "error": "sent, not found in the gallery"})
    assert row["status"] == "failed"
    assert "unconfirmed" in (row["last_error"] or "").lower()


def test_plain_failure_still_retries_inline(monkeypatch):
    row = _run(monkeypatch, {"platform": "ib", "success": False, "retry_queued": False,
                             "error": "site said no"})
    assert row["status"] == "pending"
