"""Per-account read scoping test for the Tumblr (tum) platform.

Tumblr tracks a single engagement metric (notes) and its submission_id is a
numeric id string — so this asserts on total_submissions / total_notes and the
scoped submissions list.
"""

import sqlite3

import pytest

import config
from database import accounts, tum_queries


@pytest.fixture
def conn():
    from database.db import init_db, get_connection
    config.SETTINGS_PATH.write_text("{}", encoding="utf-8")
    init_db()
    c = get_connection()
    for t in ("personas", "accounts", "tum_submissions", "tum_snapshots"):
        try:
            c.execute(f"DELETE FROM {t}")
        except sqlite3.OperationalError:
            pass
    c.commit()
    yield c
    c.close()


def test_tum_summary_and_submissions_scope_by_account(conn):
    a1 = accounts.create_account(conn, "tum", "tum-a1")
    a2 = accounts.create_account(conn, "tum", "tum-a2")
    tum_queries.upsert_tum_submission(conn, {"post_uri": "1001", "title": "A1-one", "notes": 100}, a1)
    tum_queries.upsert_tum_submission(conn, {"post_uri": "1002", "title": "A1-two", "notes": 50}, a1)
    tum_queries.upsert_tum_submission(conn, {"post_uri": "2001", "title": "A2-one", "notes": 999}, a2)
    conn.commit()

    s1 = tum_queries.get_tum_summary(conn, account_id=a1)
    assert s1["total_submissions"] == 2
    assert s1["total_notes"] == 150
    assert s1["total_favorites"] == 150          # engagement bucket for cross-platform rollup
    assert [t["submission_id"] for t in s1["top_noted"]] == ["1001", "1002"]

    s2 = tum_queries.get_tum_summary(conn, account_id=a2)
    assert s2["total_submissions"] == 1 and s2["total_notes"] == 999

    s_all = tum_queries.get_tum_summary(conn)  # All accounts
    assert s_all["total_submissions"] == 3

    assert {x["submission_id"] for x in tum_queries.get_all_tum_submissions(conn, account_id=a1)} == {"1001", "1002"}
    assert {x["submission_id"] for x in tum_queries.get_all_tum_submissions(conn)} == {"1001", "1002", "2001"}
