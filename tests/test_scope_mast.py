"""Per-account read scoping test for the Mastodon (mast) platform.

Mirrors tests/test_scope_bsky.py. Mastodon tracks likes (favourites) / reposts
(boosts) / replies (quotes is always 0), and its submission_id is a TEXT
ActivityPub URI — so this asserts on total_submissions / total_likes and the
scoped submissions list.
"""

import sqlite3

import pytest

import config
from database import accounts, mast_queries


@pytest.fixture
def conn():
    """Fresh DB with mast analytics tables cleared and settings reset to {}."""
    from database.db import init_db, get_connection
    config.SETTINGS_PATH.write_text("{}", encoding="utf-8")
    init_db()
    c = get_connection()
    for t in ("personas", "accounts", "mast_submissions", "mast_snapshots"):
        try:
            c.execute(f"DELETE FROM {t}")
        except sqlite3.OperationalError:
            pass
    c.commit()
    yield c
    c.close()


# ── per-account read scoping (Mastodon) ────────────────────────

def test_mast_summary_and_submissions_scope_by_account(conn):
    """get_mast_summary / get_all_mast_submissions filter to one account when
    given an account_id, and aggregate across accounts when not (account_id=None)."""
    a1 = accounts.create_account(conn, "mast", "mast-a1")
    a2 = accounts.create_account(conn, "mast", "mast-a2")
    mast_queries.upsert_mast_submission(conn, {"post_uri": "https://m1/users/x/statuses/101", "title": "A1-one", "likes": 100, "reposts": 5, "replies": 1, "quotes": 0}, a1)
    mast_queries.upsert_mast_submission(conn, {"post_uri": "https://m1/users/x/statuses/102", "title": "A1-two", "likes": 50, "reposts": 2, "replies": 0, "quotes": 0}, a1)
    mast_queries.upsert_mast_submission(conn, {"post_uri": "https://m2/users/y/statuses/201", "title": "A2-one", "likes": 999, "reposts": 9, "replies": 3, "quotes": 0}, a2)
    conn.commit()

    s1 = mast_queries.get_mast_summary(conn, account_id=a1)
    assert s1["total_submissions"] == 2
    assert s1["total_likes"] == 150
    assert s1["total_reposts"] == 7
    assert [t["submission_id"] for t in s1["top_liked"]] == ["https://m1/users/x/statuses/101", "https://m1/users/x/statuses/102"]

    s2 = mast_queries.get_mast_summary(conn, account_id=a2)
    assert s2["total_submissions"] == 1 and s2["total_likes"] == 999

    s_all = mast_queries.get_mast_summary(conn)  # All accounts
    assert s_all["total_submissions"] == 3

    assert {x["submission_id"] for x in mast_queries.get_all_mast_submissions(conn, account_id=a1)} == {"https://m1/users/x/statuses/101", "https://m1/users/x/statuses/102"}
    assert {x["submission_id"] for x in mast_queries.get_all_mast_submissions(conn)} == {"https://m1/users/x/statuses/101", "https://m1/users/x/statuses/102", "https://m2/users/y/statuses/201"}
