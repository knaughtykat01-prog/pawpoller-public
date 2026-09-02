"""Phase 2 per-account read scoping test for FurAffinity (FA).

Mirrors tests/test_personas.py::test_ib_summary_and_submissions_scope_by_account
for the fa_ query layer: seed two FA accounts, give each its own submissions, then
assert get_fa_summary / get_all_fa_submissions scope by account_id and aggregate
across accounts when account_id is None (the "All accounts" default).
"""

import sqlite3

import pytest

import config
from database import accounts


@pytest.fixture
def conn():
    """Fresh DB with the FA tables this test touches cleared + settings reset to {}."""
    from database.db import init_db, get_connection
    config.SETTINGS_PATH.write_text("{}", encoding="utf-8")
    init_db()
    c = get_connection()
    for t in ("accounts", "fa_submissions", "fa_snapshots", "fa_comments"):
        try:
            c.execute(f"DELETE FROM {t}")
        except sqlite3.OperationalError:
            pass
    c.commit()
    yield c
    c.close()


def test_fa_summary_and_submissions_scope_by_account(conn):
    """get_fa_summary / get_all_fa_submissions filter to one account when given an
    account_id, and aggregate across accounts when not (account_id=None)."""
    from database import fa_queries
    a1 = accounts.create_account(conn, "fa", "fa-a1")
    a2 = accounts.create_account(conn, "fa", "fa-a2")
    fa_queries.upsert_fa_submission(conn, {"submission_id": 101, "title": "A1-one", "views": 100, "favorites_count": 5, "comments_count": 1}, a1)
    fa_queries.upsert_fa_submission(conn, {"submission_id": 102, "title": "A1-two", "views": 50, "favorites_count": 2, "comments_count": 0}, a1)
    fa_queries.upsert_fa_submission(conn, {"submission_id": 201, "title": "A2-one", "views": 999, "favorites_count": 9, "comments_count": 3}, a2)
    conn.commit()

    s1 = fa_queries.get_fa_summary(conn, account_id=a1)
    assert s1["total_submissions"] == 2
    assert s1["total_views"] == 150
    assert s1["total_favorites"] == 7
    assert [t["submission_id"] for t in s1["top_viewed"]] == [101, 102]

    s2 = fa_queries.get_fa_summary(conn, account_id=a2)
    assert s2["total_submissions"] == 1 and s2["total_views"] == 999

    s_all = fa_queries.get_fa_summary(conn)  # All accounts
    assert s_all["total_submissions"] == 3

    assert {x["submission_id"] for x in fa_queries.get_all_fa_submissions(conn, account_id=a1)} == {101, 102}
    assert {x["submission_id"] for x in fa_queries.get_all_fa_submissions(conn)} == {101, 102, 201}
