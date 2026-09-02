"""Per-account read scoping for the X/Twitter (TW) platform.

Mirrors tests/test_personas.py::test_ib_summary_and_submissions_scope_by_account
for the TW query layer. Asserts that get_tw_summary / get_all_tw_submissions
filter to one account when given an account_id, and aggregate across accounts
when not (account_id=None).

TW specifics vs Inkbunny:
  - submission_id is TEXT (tweet IDs) -> string IDs, asserted as strings.
  - upsert_tw_submission reads sub["tweet_id"] for the submission_id column.
  - metrics are views / likes / retweets / replies / quotes / bookmarks
    (no favorites_count).
"""

import sqlite3

import pytest

import config
from database import accounts


@pytest.fixture
def conn():
    """Fresh DB with accounts + TW tables emptied and settings reset to {}."""
    from database.db import init_db, get_connection
    config.SETTINGS_PATH.write_text("{}", encoding="utf-8")
    init_db()
    c = get_connection()
    for t in ("personas", "accounts", "tw_submissions", "tw_snapshots", "tw_poll_log"):
        try:
            c.execute(f"DELETE FROM {t}")
        except sqlite3.OperationalError:
            pass
    c.commit()
    yield c
    c.close()


def test_tw_summary_and_submissions_scope_by_account(conn):
    """get_tw_summary / get_all_tw_submissions filter to one account when given an
    account_id, and aggregate across accounts when not (account_id=None)."""
    from database import tw_queries
    a1 = accounts.create_account(conn, "tw", "tw-a1")
    a2 = accounts.create_account(conn, "tw", "tw-a2")
    tw_queries.upsert_tw_submission(conn, {"tweet_id": "101", "title": "A1-one", "views": 100, "likes": 5, "retweets": 1}, a1)
    tw_queries.upsert_tw_submission(conn, {"tweet_id": "102", "title": "A1-two", "views": 50, "likes": 2, "retweets": 0}, a1)
    tw_queries.upsert_tw_submission(conn, {"tweet_id": "201", "title": "A2-one", "views": 999, "likes": 9, "retweets": 3}, a2)
    conn.commit()

    s1 = tw_queries.get_tw_summary(conn, account_id=a1)
    assert s1["total_submissions"] == 2
    assert s1["total_views"] == 150
    assert s1["total_likes"] == 7
    assert [t["submission_id"] for t in s1["top_viewed"]] == ["101", "102"]

    s2 = tw_queries.get_tw_summary(conn, account_id=a2)
    assert s2["total_submissions"] == 1 and s2["total_views"] == 999

    s_all = tw_queries.get_tw_summary(conn)  # All accounts
    assert s_all["total_submissions"] == 3

    assert {x["submission_id"] for x in tw_queries.get_all_tw_submissions(conn, account_id=a1)} == {"101", "102"}
    assert {x["submission_id"] for x in tw_queries.get_all_tw_submissions(conn)} == {"101", "102", "201"}
