"""Phase 2 per-account read scoping for Wattpad (WP).

Mirrors tests/test_personas.py::test_ib_summary_and_submissions_scope_by_account
but for the Wattpad query layer. Wattpad's metric columns are reads / votes /
comments_count / num_lists (no views/faves), and it has no per-user fave/comment
tracking, so only the summary totals/top-lists and the submissions list are scoped.
"""

import sqlite3

import pytest

import config
from database import accounts


@pytest.fixture
def conn():
    """Fresh DB with WP submissions/snapshots + accounts empty, settings reset to {}."""
    from database.db import init_db, get_connection
    config.SETTINGS_PATH.write_text("{}", encoding="utf-8")
    init_db()
    c = get_connection()
    for t in ("personas", "accounts", "wp_submissions", "wp_snapshots"):
        try:
            c.execute(f"DELETE FROM {t}")
        except sqlite3.OperationalError:
            pass
    c.commit()
    yield c
    c.close()


# ── Phase 2: per-account read scoping (Wattpad) ────────────────

def test_wp_summary_and_submissions_scope_by_account(conn):
    """get_wp_summary / get_all_wp_submissions filter to one account when given an
    account_id, and aggregate across accounts when not (account_id=None)."""
    from database import wp_queries
    a1 = accounts.create_account(conn, "wp", "wp-a1")
    a2 = accounts.create_account(conn, "wp", "wp-a2")
    # upsert_wp_submission reads sub["story_id"] for the submission_id column.
    wp_queries.upsert_wp_submission(conn, {"story_id": 101, "title": "A1-one", "reads": 100, "votes": 5, "comments_count": 1, "num_lists": 2}, a1)
    wp_queries.upsert_wp_submission(conn, {"story_id": 102, "title": "A1-two", "reads": 50, "votes": 2, "comments_count": 0, "num_lists": 1}, a1)
    wp_queries.upsert_wp_submission(conn, {"story_id": 201, "title": "A2-one", "reads": 999, "votes": 9, "comments_count": 3, "num_lists": 7}, a2)
    conn.commit()

    s1 = wp_queries.get_wp_summary(conn, account_id=a1)
    assert s1["total_submissions"] == 2
    assert s1["total_reads"] == 150
    assert s1["total_votes"] == 7
    assert [t["submission_id"] for t in s1["top_read"]] == [101, 102]

    s2 = wp_queries.get_wp_summary(conn, account_id=a2)
    assert s2["total_submissions"] == 1 and s2["total_reads"] == 999

    s_all = wp_queries.get_wp_summary(conn)  # All accounts
    assert s_all["total_submissions"] == 3

    assert {x["submission_id"] for x in wp_queries.get_all_wp_submissions(conn, account_id=a1)} == {101, 102}
    assert {x["submission_id"] for x in wp_queries.get_all_wp_submissions(conn)} == {101, 102, 201}
