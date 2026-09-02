"""Per-account read scoping test for the Bluesky (bsky) platform.

Mirrors tests/test_personas.py::test_ib_summary_and_submissions_scope_by_account.
Bluesky tracks likes / reposts / replies / quotes (NO views, and no individual
comment/fave tracking), and its submission_id is a TEXT AT URI — so this asserts
on total_submissions / total_likes and the scoped submissions list.
"""

import sqlite3

import pytest

import config
from database import accounts, bsky_queries


@pytest.fixture
def conn():
    """Fresh DB with bsky analytics tables cleared and settings reset to {}."""
    from database.db import init_db, get_connection
    config.SETTINGS_PATH.write_text("{}", encoding="utf-8")
    init_db()
    c = get_connection()
    for t in ("personas", "accounts", "bsky_submissions", "bsky_snapshots"):
        try:
            c.execute(f"DELETE FROM {t}")
        except sqlite3.OperationalError:
            pass
    c.commit()
    yield c
    c.close()


# ── Phase 2: per-account read scoping (Bluesky) ────────────────

def test_bsky_summary_and_submissions_scope_by_account(conn):
    """get_bsky_summary / get_all_bsky_submissions filter to one account when
    given an account_id, and aggregate across accounts when not (account_id=None)."""
    a1 = accounts.create_account(conn, "bsky", "bsky-a1")
    a2 = accounts.create_account(conn, "bsky", "bsky-a2")
    bsky_queries.upsert_bsky_submission(conn, {"post_uri": "at://a1/post/101", "title": "A1-one", "likes": 100, "reposts": 5, "replies": 1, "quotes": 0}, a1)
    bsky_queries.upsert_bsky_submission(conn, {"post_uri": "at://a1/post/102", "title": "A1-two", "likes": 50, "reposts": 2, "replies": 0, "quotes": 0}, a1)
    bsky_queries.upsert_bsky_submission(conn, {"post_uri": "at://a2/post/201", "title": "A2-one", "likes": 999, "reposts": 9, "replies": 3, "quotes": 0}, a2)
    conn.commit()

    s1 = bsky_queries.get_bsky_summary(conn, account_id=a1)
    assert s1["total_submissions"] == 2
    assert s1["total_likes"] == 150
    assert s1["total_reposts"] == 7
    assert [t["submission_id"] for t in s1["top_liked"]] == ["at://a1/post/101", "at://a1/post/102"]

    s2 = bsky_queries.get_bsky_summary(conn, account_id=a2)
    assert s2["total_submissions"] == 1 and s2["total_likes"] == 999

    s_all = bsky_queries.get_bsky_summary(conn)  # All accounts
    assert s_all["total_submissions"] == 3

    assert {x["submission_id"] for x in bsky_queries.get_all_bsky_submissions(conn, account_id=a1)} == {"at://a1/post/101", "at://a1/post/102"}
    assert {x["submission_id"] for x in bsky_queries.get_all_bsky_submissions(conn)} == {"at://a1/post/101", "at://a1/post/102", "at://a2/post/201"}
