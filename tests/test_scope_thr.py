"""Per-account read scoping test for the Threads (thr) platform.

Threads tracks views / likes / reposts / replies / quotes; submission_id is a
numeric media id. Asserts on totals + the scoped submissions list.
"""

import sqlite3

import pytest

import config
from database import accounts, thr_queries


@pytest.fixture
def conn():
    from database.db import init_db, get_connection
    config.SETTINGS_PATH.write_text("{}", encoding="utf-8")
    init_db()
    c = get_connection()
    for t in ("personas", "accounts", "thr_submissions", "thr_snapshots"):
        try:
            c.execute(f"DELETE FROM {t}")
        except sqlite3.OperationalError:
            pass
    c.commit()
    yield c
    c.close()


def test_thr_summary_and_submissions_scope_by_account(conn):
    a1 = accounts.create_account(conn, "thr", "thr-a1")
    a2 = accounts.create_account(conn, "thr", "thr-a2")
    thr_queries.upsert_thr_submission(conn, {"post_uri": "101", "title": "A1-one", "views": 1000, "likes": 100, "reposts": 5, "replies": 8, "quotes": 1}, a1)
    thr_queries.upsert_thr_submission(conn, {"post_uri": "102", "title": "A1-two", "views": 500, "likes": 50, "reposts": 2, "replies": 3, "quotes": 0}, a1)
    thr_queries.upsert_thr_submission(conn, {"post_uri": "201", "title": "A2-one", "views": 9999, "likes": 900, "reposts": 30, "replies": 40, "quotes": 9}, a2)
    conn.commit()

    s1 = thr_queries.get_thr_summary(conn, account_id=a1)
    assert s1["total_submissions"] == 2
    assert s1["total_views"] == 1500
    assert s1["total_likes"] == 150
    assert s1["total_favorites"] == 150          # engagement bucket for cross-platform rollup
    assert [t["submission_id"] for t in s1["top_viewed"]] == ["101", "102"]

    s2 = thr_queries.get_thr_summary(conn, account_id=a2)
    assert s2["total_submissions"] == 1 and s2["total_views"] == 9999

    s_all = thr_queries.get_thr_summary(conn)  # All accounts
    assert s_all["total_submissions"] == 3

    assert {x["submission_id"] for x in thr_queries.get_all_thr_submissions(conn, account_id=a1)} == {"101", "102"}
    assert {x["submission_id"] for x in thr_queries.get_all_thr_submissions(conn)} == {"101", "102", "201"}
