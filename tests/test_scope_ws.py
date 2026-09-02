"""Per-account read scoping for the Weasyl (WS) platform.

Mirrors test_personas.py::test_ib_summary_and_submissions_scope_by_account but
on ws_submissions / ws_snapshots. Confirms get_ws_summary / get_all_ws_submissions
filter to one account when given an account_id, and aggregate across accounts when
not (account_id=None).
"""

import sqlite3

import pytest

import config
from database import accounts


@pytest.fixture
def conn():
    """Fresh DB with the WS analytics tables cleared and settings reset to {}."""
    from database.db import init_db, get_connection
    config.SETTINGS_PATH.write_text("{}", encoding="utf-8")
    init_db()
    c = get_connection()
    for t in ("personas", "accounts", "ws_submissions", "ws_snapshots", "ws_poll_log"):
        try:
            c.execute(f"DELETE FROM {t}")
        except sqlite3.OperationalError:
            pass
    c.commit()
    yield c
    c.close()


def test_ws_summary_and_submissions_scope_by_account(conn):
    """get_ws_summary / get_all_ws_submissions filter to one account when given an
    account_id, and aggregate across accounts when not (account_id=None)."""
    from database import ws_queries
    a1 = accounts.create_account(conn, "ws", "ws-a1")
    a2 = accounts.create_account(conn, "ws", "ws-a2")
    ws_queries.upsert_ws_submission(conn, {"submission_id": 101, "title": "A1-one", "views": 100, "favorites_count": 5, "comments_count": 1}, a1)
    ws_queries.upsert_ws_submission(conn, {"submission_id": 102, "title": "A1-two", "views": 50, "favorites_count": 2, "comments_count": 0}, a1)
    ws_queries.upsert_ws_submission(conn, {"submission_id": 201, "title": "A2-one", "views": 999, "favorites_count": 9, "comments_count": 3}, a2)
    conn.commit()

    s1 = ws_queries.get_ws_summary(conn, account_id=a1)
    assert s1["total_submissions"] == 2
    assert s1["total_views"] == 150
    assert s1["total_favorites"] == 7
    assert [t["submission_id"] for t in s1["top_viewed"]] == [101, 102]

    s2 = ws_queries.get_ws_summary(conn, account_id=a2)
    assert s2["total_submissions"] == 1 and s2["total_views"] == 999

    s_all = ws_queries.get_ws_summary(conn)  # All accounts
    assert s_all["total_submissions"] == 3

    assert {x["submission_id"] for x in ws_queries.get_all_ws_submissions(conn, account_id=a1)} == {101, 102}
    assert {x["submission_id"] for x in ws_queries.get_all_ws_submissions(conn)} == {101, 102, 201}
