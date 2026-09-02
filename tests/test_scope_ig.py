"""Per-account read scoping test for the Instagram (ig) platform.

Instagram tracks views / reach / likes / comments / saved / shares; submission_id
is a numeric media id. Asserts on totals + the scoped submissions list.
"""

import sqlite3

import pytest

import config
from database import accounts, ig_queries


@pytest.fixture
def conn():
    from database.db import init_db, get_connection
    config.SETTINGS_PATH.write_text("{}", encoding="utf-8")
    init_db()
    c = get_connection()
    for t in ("personas", "accounts", "ig_submissions", "ig_snapshots"):
        try:
            c.execute(f"DELETE FROM {t}")
        except sqlite3.OperationalError:
            pass
    c.commit()
    yield c
    c.close()


def test_ig_summary_and_submissions_scope_by_account(conn):
    a1 = accounts.create_account(conn, "ig", "ig-a1")
    a2 = accounts.create_account(conn, "ig", "ig-a2")
    ig_queries.upsert_ig_submission(conn, {"post_uri": "101", "title": "A1-one", "views": 1000, "reach": 800, "likes": 100, "comments": 8, "saved": 5, "shares": 1}, a1)
    ig_queries.upsert_ig_submission(conn, {"post_uri": "102", "title": "A1-two", "views": 500, "reach": 400, "likes": 50, "comments": 3, "saved": 2, "shares": 0}, a1)
    ig_queries.upsert_ig_submission(conn, {"post_uri": "201", "title": "A2-one", "views": 9999, "reach": 7000, "likes": 900, "comments": 40, "saved": 30, "shares": 9}, a2)
    conn.commit()

    s1 = ig_queries.get_ig_summary(conn, account_id=a1)
    assert s1["total_submissions"] == 2
    assert s1["total_views"] == 1500
    assert s1["total_likes"] == 150
    assert s1["total_favorites"] == 150          # engagement bucket for cross-platform rollup
    assert [t["submission_id"] for t in s1["top_viewed"]] == ["101", "102"]

    s2 = ig_queries.get_ig_summary(conn, account_id=a2)
    assert s2["total_submissions"] == 1 and s2["total_views"] == 9999

    s_all = ig_queries.get_ig_summary(conn)  # All accounts
    assert s_all["total_submissions"] == 3

    assert {x["submission_id"] for x in ig_queries.get_all_ig_submissions(conn, account_id=a1)} == {"101", "102"}
    assert {x["submission_id"] for x in ig_queries.get_all_ig_submissions(conn)} == {"101", "102", "201"}
