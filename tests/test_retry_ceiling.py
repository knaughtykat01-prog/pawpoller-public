"""The retry ceiling, and what a retry is allowed to change (3.21.0).

`_schedule_retry` has always looked like it stopped after three attempts::

    max_attempts = 3
    if attempt >= max_attempts:
        ...give up

but `attempt` was a parameter and all three call sites passed the literal `0`.
`0 >= 3` is never true, so every failure queued another row, for ever. By the
time anyone looked the queue held **919 DeviantArt rows, 3,208 AO3 rows and 267
Itaku rows** — roughly 4,400 rows of the same handful of jobs failing over and
over. The DeviantArt loop was re-calling DA's OAuth endpoint every five seconds
and had been doing so for three days.

Three separate defects had to line up, and each one is pinned below:

  1. the ceiling was unreachable (a hardcoded attempt number);
  2. an unfixable error was retried at all — a dead credential cannot be fixed
     by trying again, only by a person pasting a new one;
  3. the retry row was queued with no ``account_id``, so `add_to_queue` filled
     in the platform DEFAULT. A post that failed as account 27 came back as
     account 7 — and had it then succeeded it would have published to the wrong
     gallery. Every one of those 919 DeviantArt rows had been re-pointed that
     way.

(3) is the quiet one. It is the same failure the Bluesky wrong-account post was:
work arriving somewhere nobody chose.
"""
from __future__ import annotations

import pytest

from database import posting_queries
from database.db import get_connection
from posting.manager import _schedule_retry


def _rows(conn, platform="da"):
    return [dict(r) for r in conn.execute(
        "SELECT queue_id, account_id, priority, scheduled_at FROM posting_queue "
        "WHERE platform = ? ORDER BY queue_id", (platform,))]


# ── 1. the ceiling is real ───────────────────────────────────────────

def test_retries_stop_at_three():
    """Fail the same job over and over; the queue must not grow without end."""
    for _ in range(10):
        _schedule_retry("Sitting_Serious", 0, "da", "post",
                        "HTTP 503 upstream hiccup",
                        content_type="artwork", account_id=7)

    conn = get_connection()
    try:
        rows = _rows(conn)
    finally:
        conn.close()
    assert len(rows) == 3, (
        f"expected the 3-attempt ceiling, got {len(rows)} rows — this is how "
        "one dead token became 919 queue rows")


def test_the_fourth_failure_reports_that_it_gave_up(caplog):
    import logging
    for _ in range(3):
        _schedule_retry("X", 0, "da", "post", "boom", account_id=7)
    with caplog.at_level(logging.INFO):
        queued = _schedule_retry("X", 0, "da", "post", "boom", account_id=7)
    assert queued is False
    assert "max attempts" in caplog.text


def test_the_backoff_lengthens_with_each_attempt():
    """1 min, then 5, then 30 — the delays only mean anything if the attempt
    number is real."""
    for _ in range(3):
        _schedule_retry("X", 0, "da", "post", "boom", account_id=7)
    conn = get_connection()
    try:
        stamps = [r["scheduled_at"] for r in _rows(conn)]
    finally:
        conn.close()
    assert stamps == sorted(stamps) and len(set(stamps)) == 3


def test_a_hand_queued_row_starts_a_fresh_campaign():
    """Three old failures must not bar the job for ever — re-queueing it by
    hand (priority >= 0) resets the count, or the ceiling would turn every
    transient outage into a permanent ban."""
    for _ in range(5):
        _schedule_retry("X", 0, "da", "post", "boom", account_id=7)

    conn = get_connection()
    try:
        assert posting_queries.count_retry_rows(
            conn, "X", 0, "da", "post", account_id=7) == 3
        posting_queries.add_to_queue(conn, "X", 0, "da", "post", account_id=7,
                                     scheduled_at="2030-01-01 00:00:00")
        assert posting_queries.count_retry_rows(
            conn, "X", 0, "da", "post", account_id=7) == 0
    finally:
        conn.close()

    assert _schedule_retry("X", 0, "da", "post", "boom", account_id=7) is True


def test_the_count_is_per_account_and_per_target():
    """Two accounts failing the same artwork are two campaigns, not one."""
    for _ in range(3):
        _schedule_retry("Same_Art", 0, "da", "post", "boom",
                        content_type="artwork", account_id=7)
    assert _schedule_retry("Same_Art", 0, "da", "post", "boom",
                           content_type="artwork", account_id=27) is True
    assert _schedule_retry("Other_Art", 0, "da", "post", "boom",
                           content_type="artwork", account_id=7) is True


# ── 2. dead credentials are never retried ────────────────────────────

@pytest.mark.parametrize("error", [
    "DA: the stored refresh token is no longer valid — re-authorise this account",
    'DA: Token refresh failed — 400: {"error":"invalid_request",'
    '"error_description":"The refresh_token is invalid."}',
    "FA: not logged in — the session cookies (a/b) are expired or invalid. "
    "Re-copy them from a signed-in browser.",
    "DeviantArt OAuth not configured. Set da_client_id",
])
def test_a_dead_credential_is_never_retried(error):
    """No number of attempts fixes a credential. Retrying one only queues rows
    and hammers the platform's auth endpoint on the way."""
    assert _schedule_retry("X", 0, "da", "post", error, account_id=7) is False
    conn = get_connection()
    try:
        assert _rows(conn) == []
    finally:
        conn.close()


def test_a_transient_failure_is_still_retried():
    """The classifier must not swallow ordinary errors — that would be a worse
    bug than the one being fixed."""
    assert _schedule_retry("X", 0, "sf", "post",
                           "HTTP 503 upstream hiccup", account_id=1) is True


def test_the_permanent_message_tells_the_operator_what_to_do(caplog):
    import logging
    with caplog.at_level(logging.WARNING):
        _schedule_retry("X", 0, "da", "post",
                        "DA: the stored refresh token is no longer valid — "
                        "re-authorise this account", account_id=7)
    assert "NOT retrying" in caplog.text
    assert "Settings" in caplog.text


# ── 3. a retry never changes which account posts ─────────────────────

def test_a_retry_keeps_the_account_that_failed():
    """THE quiet one. Queued without account_id, add_to_queue substitutes the
    platform default — so account 27's failure would come back as account 7 and
    publish to the wrong gallery."""
    _schedule_retry("Sitting_Serious", 0, "da", "post", "boom",
                    content_type="artwork", account_id=27)
    conn = get_connection()
    try:
        rows = _rows(conn)
    finally:
        conn.close()
    assert len(rows) == 1
    assert rows[0]["account_id"] == 27, (
        f"retry was re-pointed at account {rows[0]['account_id']} — a successful "
        "retry would then post to the wrong account's gallery")


def test_a_retry_row_is_marked_as_a_retry():
    """priority = -1 is what count_retry_rows counts; lose it and the ceiling
    silently stops working again."""
    _schedule_retry("X", 0, "da", "post", "boom", account_id=7)
    conn = get_connection()
    try:
        assert _rows(conn)[0]["priority"] == -1
    finally:
        conn.close()


def test_no_caller_hardcodes_the_attempt_number():
    """The original bug in its general form: an attempt counter that the caller
    supplies is a counter that the caller will get wrong. It is now derived from
    the queue and there is no parameter left to pass — this pins that shape."""
    import inspect
    from posting import manager

    sig = inspect.signature(manager._schedule_retry)
    assert "attempt" not in sig.parameters, (
        "attempt must stay derived from the queue; a parameter is what let "
        "all three call sites pass a literal 0")
