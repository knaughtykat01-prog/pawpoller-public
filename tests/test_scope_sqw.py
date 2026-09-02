"""Phase 2: per-account read scoping for SquidgeWorld (sqw).

Mirrors the Inkbunny scoping test in tests/test_personas.py
(``test_ib_summary_and_submissions_scope_by_account``): seed two sqw accounts,
insert distinct works for each, then assert get_sqw_summary /
get_all_sqw_submissions filter to one account when given an account_id and
aggregate across accounts when not (account_id=None).
"""

import sqlite3

import pytest

import config
from database import accounts


@pytest.fixture
def conn():
    """Fresh DB with sqw tables + accounts empty and settings reset to {}."""
    from database.db import init_db, get_connection
    config.SETTINGS_PATH.write_text("{}", encoding="utf-8")
    init_db()
    c = get_connection()
    for t in ("personas", "accounts", "sqw_submissions", "sqw_snapshots",
              "sqw_kudos_users", "sqw_poll_log"):
        try:
            c.execute(f"DELETE FROM {t}")
        except sqlite3.OperationalError:
            pass
    c.commit()
    yield c
    c.close()


def test_sqw_summary_and_submissions_scope_by_account(conn):
    """get_sqw_summary / get_all_sqw_submissions filter to one account when given
    an account_id, and aggregate across accounts when not (account_id=None)."""
    from database import sqw_queries
    a1 = accounts.create_account(conn, "sqw", "sqw-a1")
    a2 = accounts.create_account(conn, "sqw", "sqw-a2")
    # NB: upsert_sqw_submission reads sub["work_id"] (-> submission_id column).
    sqw_queries.upsert_sqw_submission(conn, {"work_id": 101, "title": "A1-one", "views": 100, "favorites_count": 5, "comments_count": 1, "bookmarks_count": 2}, a1)
    sqw_queries.upsert_sqw_submission(conn, {"work_id": 102, "title": "A1-two", "views": 50, "favorites_count": 2, "comments_count": 0, "bookmarks_count": 1}, a1)
    sqw_queries.upsert_sqw_submission(conn, {"work_id": 201, "title": "A2-one", "views": 999, "favorites_count": 9, "comments_count": 3, "bookmarks_count": 7}, a2)
    conn.commit()

    s1 = sqw_queries.get_sqw_summary(conn, account_id=a1)
    assert s1["total_submissions"] == 2
    assert s1["total_views"] == 150
    assert s1["total_favorites"] == 7
    assert s1["total_bookmarks"] == 3
    assert [t["submission_id"] for t in s1["top_viewed"]] == [101, 102]

    s2 = sqw_queries.get_sqw_summary(conn, account_id=a2)
    assert s2["total_submissions"] == 1 and s2["total_views"] == 999

    s_all = sqw_queries.get_sqw_summary(conn)  # All accounts
    assert s_all["total_submissions"] == 3

    assert {x["submission_id"] for x in sqw_queries.get_all_sqw_submissions(conn, account_id=a1)} == {101, 102}
    assert {x["submission_id"] for x in sqw_queries.get_all_sqw_submissions(conn)} == {101, 102, 201}
