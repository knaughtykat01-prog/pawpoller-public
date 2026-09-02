"""Per-account read scoping test for the Pixiv (pix) platform.

Pixiv tracks the gallery shape (views / favorites_count / comments_count); its
submission_id is a namespaced id ("illust:123"). Asserts on totals + the scoped
submissions list.
"""

import sqlite3

import pytest

import config
from database import accounts, pix_queries


@pytest.fixture
def conn():
    from database.db import init_db, get_connection
    config.SETTINGS_PATH.write_text("{}", encoding="utf-8")
    init_db()
    c = get_connection()
    for t in ("personas", "accounts", "pix_submissions", "pix_snapshots"):
        try:
            c.execute(f"DELETE FROM {t}")
        except sqlite3.OperationalError:
            pass
    c.commit()
    yield c
    c.close()


def test_pix_summary_and_submissions_scope_by_account(conn):
    a1 = accounts.create_account(conn, "pix", "pix-a1")
    a2 = accounts.create_account(conn, "pix", "pix-a2")
    pix_queries.upsert_pix_submission(conn, {"post_uri": "illust:101", "title": "A1-one", "views": 1000, "favorites_count": 100, "comments_count": 5}, a1)
    pix_queries.upsert_pix_submission(conn, {"post_uri": "novel:102", "title": "A1-two", "views": 500, "favorites_count": 50, "comments_count": 2}, a1)
    pix_queries.upsert_pix_submission(conn, {"post_uri": "illust:201", "title": "A2-one", "views": 9999, "favorites_count": 900, "comments_count": 30}, a2)
    conn.commit()

    s1 = pix_queries.get_pix_summary(conn, account_id=a1)
    assert s1["total_submissions"] == 2
    assert s1["total_views"] == 1500
    assert s1["total_favorites"] == 150
    assert [t["submission_id"] for t in s1["top_viewed"]] == ["illust:101", "novel:102"]

    s2 = pix_queries.get_pix_summary(conn, account_id=a2)
    assert s2["total_submissions"] == 1 and s2["total_views"] == 9999

    s_all = pix_queries.get_pix_summary(conn)  # All accounts
    assert s_all["total_submissions"] == 3

    assert {x["submission_id"] for x in pix_queries.get_all_pix_submissions(conn, account_id=a1)} == {"illust:101", "novel:102"}
    assert {x["submission_id"] for x in pix_queries.get_all_pix_submissions(conn)} == {"illust:101", "novel:102", "illust:201"}
