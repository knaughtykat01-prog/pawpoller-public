"""Phase 2 per-account read scoping for AO3 (mirrors the verified Inkbunny test).

Asserts that ``get_ao3_summary`` / ``get_all_ao3_submissions`` filter to one
account when given an ``account_id``, and aggregate across accounts when not
(``account_id=None`` — the "All accounts" default). The conn fixture mirrors
``tests/test_personas.py`` but adds the AO3 analytics tables to its DELETE list.
"""

import sqlite3

import pytest

import config
from database import accounts, ao3_queries


@pytest.fixture
def conn():
    """Fresh DB with accounts + AO3 analytics tables empty and settings reset."""
    from database.db import init_db, get_connection
    config.SETTINGS_PATH.write_text("{}", encoding="utf-8")
    init_db()
    c = get_connection()
    for t in ("personas", "accounts", "ao3_submissions", "ao3_snapshots",
              "ao3_kudos_users"):
        try:
            c.execute(f"DELETE FROM {t}")
        except sqlite3.OperationalError:
            pass
    c.commit()
    yield c
    c.close()


def test_ao3_summary_and_submissions_scope_by_account(conn):
    """get_ao3_summary / get_all_ao3_submissions filter to one account when given
    an account_id, and aggregate across accounts when not (account_id=None)."""
    a1 = accounts.create_account(conn, "ao3", "ao3-a1")
    a2 = accounts.create_account(conn, "ao3", "ao3-a2")
    # AO3 upsert keys the work on `work_id`; metrics are views (hits),
    # favorites_count (kudos), comments_count, bookmarks_count.
    ao3_queries.upsert_ao3_submission(conn, {"work_id": 101, "title": "A1-one", "views": 100, "favorites_count": 5, "comments_count": 1, "bookmarks_count": 4}, a1)
    ao3_queries.upsert_ao3_submission(conn, {"work_id": 102, "title": "A1-two", "views": 50, "favorites_count": 2, "comments_count": 0, "bookmarks_count": 1}, a1)
    ao3_queries.upsert_ao3_submission(conn, {"work_id": 201, "title": "A2-one", "views": 999, "favorites_count": 9, "comments_count": 3, "bookmarks_count": 7}, a2)
    conn.commit()

    s1 = ao3_queries.get_ao3_summary(conn, account_id=a1)
    assert s1["total_submissions"] == 2
    assert s1["total_views"] == 150
    assert s1["total_favorites"] == 7
    assert s1["total_bookmarks"] == 5
    assert [t["submission_id"] for t in s1["top_viewed"]] == [101, 102]

    s2 = ao3_queries.get_ao3_summary(conn, account_id=a2)
    assert s2["total_submissions"] == 1 and s2["total_views"] == 999

    s_all = ao3_queries.get_ao3_summary(conn)  # All accounts
    assert s_all["total_submissions"] == 3
    assert s_all["total_views"] == 1149

    assert {x["submission_id"] for x in ao3_queries.get_all_ao3_submissions(conn, account_id=a1)} == {101, 102}
    assert {x["submission_id"] for x in ao3_queries.get_all_ao3_submissions(conn)} == {101, 102, 201}
