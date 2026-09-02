"""Phase 2 per-account read scoping test for DeviantArt (DA).

Mirrors tests/test_personas.py::test_ib_summary_and_submissions_scope_by_account
for the DA platform: get_da_summary / get_all_da_submissions filter to one
account when given an account_id, and aggregate across accounts when not
(account_id=None). DA's extra metric (downloads) is checked too.
"""

import sqlite3

import pytest

import config
from database import accounts


@pytest.fixture
def conn():
    """Fresh DB with DA tables cleared and settings reset to {}."""
    from database.db import init_db, get_connection
    config.SETTINGS_PATH.write_text("{}", encoding="utf-8")
    init_db()
    c = get_connection()
    for t in ("personas", "accounts", "da_submissions", "da_snapshots"):
        try:
            c.execute(f"DELETE FROM {t}")
        except sqlite3.OperationalError:
            pass
    c.commit()
    yield c
    c.close()


def test_da_summary_and_submissions_scope_by_account(conn):
    """get_da_summary / get_all_da_submissions filter to one account when given an
    account_id, and aggregate across accounts when not (account_id=None)."""
    from database import da_queries
    a1 = accounts.create_account(conn, "da", "da-a1")
    a2 = accounts.create_account(conn, "da", "da-a2")
    # DA's upsert keys the submission_id off `deviation_id` and tracks an extra
    # `downloads` metric alongside views/favorites_count/comments_count.
    da_queries.upsert_da_submission(conn, {"deviation_id": 101, "title": "A1-one", "views": 100, "favorites_count": 5, "comments_count": 1, "downloads": 2}, a1)
    da_queries.upsert_da_submission(conn, {"deviation_id": 102, "title": "A1-two", "views": 50, "favorites_count": 2, "comments_count": 0, "downloads": 1}, a1)
    da_queries.upsert_da_submission(conn, {"deviation_id": 201, "title": "A2-one", "views": 999, "favorites_count": 9, "comments_count": 3, "downloads": 7}, a2)
    conn.commit()

    s1 = da_queries.get_da_summary(conn, account_id=a1)
    assert s1["total_submissions"] == 2
    assert s1["total_views"] == 150
    assert s1["total_favorites"] == 7
    assert s1["total_downloads"] == 3
    assert [t["submission_id"] for t in s1["top_viewed"]] == [101, 102]

    s2 = da_queries.get_da_summary(conn, account_id=a2)
    assert s2["total_submissions"] == 1 and s2["total_views"] == 999

    s_all = da_queries.get_da_summary(conn)  # All accounts
    assert s_all["total_submissions"] == 3
    assert s_all["total_downloads"] == 10

    assert {x["submission_id"] for x in da_queries.get_all_da_submissions(conn, account_id=a1)} == {101, 102}
    assert {x["submission_id"] for x in da_queries.get_all_da_submissions(conn)} == {101, 102, 201}
