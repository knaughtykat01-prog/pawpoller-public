"""Scheduler & queue diagnostics.

Thread liveness + queue regression tests. The two queue tests insert
a marker row and clean it up in finally.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import config
from database.db import get_connection
from database import posting_queries
from testing.registry import TestContext, register_test


def _named_thread_alive(needle: str) -> bool:
    for t in threading.enumerate():
        if needle.lower() in t.name.lower() and t.is_alive():
            return True
    return False


@register_test(
    test_id="scheduling.poll_orchestrator_alive",
    name="Poll orchestrator thread alive",
    category="Scheduling & Queue",
    description="A daemon thread named like 'Poll orchestrator' or '... poller' should be running.",
)
async def t_poll_orch(ctx: TestContext) -> None:
    names = [t.name for t in threading.enumerate() if t.is_alive()]
    ctx.detail("threads", names)
    ok = (
        _named_thread_alive("poll orchestrator")
        or _named_thread_alive("ib poller")
        or _named_thread_alive("polling")
    )
    assert ok, "no poller / orchestrator thread alive"


@register_test(
    test_id="scheduling.posting_scheduler_alive",
    name="Posting scheduler thread alive",
    category="Scheduling & Queue",
    description="Thread named 'Posting scheduler' should be running.",
)
async def t_posting_sched(ctx: TestContext) -> None:
    ok = _named_thread_alive("Posting scheduler")
    ctx.detail("alive", ok)
    assert ok, "Posting scheduler thread not running"


@register_test(
    test_id="scheduling.telegram_bot_alive",
    name="Telegram bot thread alive (if enabled)",
    category="Scheduling & Queue",
    description="When telegram_enabled, the bot poll thread should be up.",
)
async def t_tg_bot(ctx: TestContext) -> None:
    s = config.get_settings()
    if not s.get("telegram_enabled"):
        raise ctx.skip("telegram_enabled is false")
    ok = _named_thread_alive("telegram")
    ctx.detail("alive", ok)
    assert ok, "Telegram bot thread not running"


@register_test(
    test_id="scheduling.queue.requires_filter_regression",
    name="Queue: requires='desktop' filtered on server (regression 2.18.16)",
    category="Scheduling & Queue",
    description=(
        "Insert a marker row with requires='desktop', call get_pending_queue "
        "with runtime_mode='server', confirm the marker is excluded. Clean up."
    ),
)
async def t_queue_requires(ctx: TestContext) -> None:
    conn = get_connection()
    inserted_id: int | None = None
    try:
        inserted_id = posting_queries.add_to_queue(
            conn,
            story_name="_diagnostic_marker",
            chapter_index=0,
            platform="ib",
            action="post",
            requires="desktop",
        )
        ctx.detail("inserted_id", inserted_id)
        server_items = posting_queries.get_pending_queue(conn, limit=100, runtime_mode="server")
        server_ids = [it["queue_id"] for it in server_items]
        ctx.detail("server_visible_count", len(server_ids))
        assert inserted_id not in server_ids, (
            "desktop-only row visible to server mode (regression!)"
        )
        desktop_items = posting_queries.get_pending_queue(conn, limit=100, runtime_mode="desktop")
        desktop_ids = [it["queue_id"] for it in desktop_items]
        ctx.detail("desktop_visible_count", len(desktop_ids))
        assert inserted_id in desktop_ids, "desktop-only row not visible to desktop mode"
    finally:
        if inserted_id is not None:
            conn.execute("DELETE FROM posting_queue WHERE queue_id = ?", (inserted_id,))
            conn.commit()
        conn.close()


@register_test(
    test_id="scheduling.queue.scheduled_at_gate",
    name="Queue: future scheduled_at not yet eligible",
    category="Scheduling & Queue",
    description="A row scheduled an hour from now should NOT appear in get_pending_queue.",
)
async def t_queue_scheduled(ctx: TestContext) -> None:
    conn = get_connection()
    inserted_id: int | None = None
    try:
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        inserted_id = posting_queries.add_to_queue(
            conn,
            story_name="_diagnostic_future_marker",
            chapter_index=0,
            platform="ib",
            action="post",
            scheduled_at=future,
        )
        ctx.detail("inserted_id", inserted_id)
        ctx.detail("scheduled_at", future)
        items = posting_queries.get_pending_queue(conn, limit=100)
        ids = [it["queue_id"] for it in items]
        ctx.detail("visible_count", len(ids))
        assert inserted_id not in ids, "future-scheduled row appeared in pending list"
    finally:
        if inserted_id is not None:
            conn.execute("DELETE FROM posting_queue WHERE queue_id = ?", (inserted_id,))
            conn.commit()
        conn.close()
