"""Per-account read scoping tests for the SoFurry (sf) platform.

Mirrors tests/test_personas.py::test_ib_summary_and_submissions_scope_by_account
for the SF query layer. SF submission_ids are TEXT (alphanumeric slugs), and SF
tracks counts only (no individual fave/comment rows), so this covers the totals,
top-lists, and the submissions list — the surfaces the account_id filter touches.
"""

import sqlite3

import pytest

import config
from database import accounts


@pytest.fixture
def conn():
    """Fresh DB with accounts + SF tables emptied and settings reset to {}."""
    from database.db import init_db, get_connection
    config.SETTINGS_PATH.write_text("{}", encoding="utf-8")
    init_db()
    c = get_connection()
    for t in ("personas", "accounts", "sf_submissions", "sf_snapshots"):
        try:
            c.execute(f"DELETE FROM {t}")
        except sqlite3.OperationalError:
            pass
    c.commit()
    yield c
    c.close()


def test_sf_summary_and_submissions_scope_by_account(conn):
    """get_sf_summary / get_all_sf_submissions filter to one account when given an
    account_id, and aggregate across accounts when not (account_id=None)."""
    from database import sf_queries
    a1 = accounts.create_account(conn, "sf", "sf-a1")
    a2 = accounts.create_account(conn, "sf", "sf-a2")
    sf_queries.upsert_sf_submission(conn, {"submission_id": "sfA1one", "title": "A1-one", "views": 100, "favorites_count": 5, "comments_count": 1}, a1)
    sf_queries.upsert_sf_submission(conn, {"submission_id": "sfA1two", "title": "A1-two", "views": 50, "favorites_count": 2, "comments_count": 0}, a1)
    sf_queries.upsert_sf_submission(conn, {"submission_id": "sfA2one", "title": "A2-one", "views": 999, "favorites_count": 9, "comments_count": 3}, a2)
    conn.commit()

    s1 = sf_queries.get_sf_summary(conn, account_id=a1)
    assert s1["total_submissions"] == 2
    assert s1["total_views"] == 150
    assert s1["total_favorites"] == 7
    assert [t["submission_id"] for t in s1["top_viewed"]] == ["sfA1one", "sfA1two"]

    s2 = sf_queries.get_sf_summary(conn, account_id=a2)
    assert s2["total_submissions"] == 1 and s2["total_views"] == 999

    s_all = sf_queries.get_sf_summary(conn)  # All accounts
    assert s_all["total_submissions"] == 3

    assert {x["submission_id"] for x in sf_queries.get_all_sf_submissions(conn, account_id=a1)} == {"sfA1one", "sfA1two"}
    assert {x["submission_id"] for x in sf_queries.get_all_sf_submissions(conn)} == {"sfA1one", "sfA1two", "sfA2one"}
