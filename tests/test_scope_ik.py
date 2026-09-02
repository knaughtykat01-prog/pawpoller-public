"""Per-account read scoping for Itaku (IK) — mirrors the IB scope test.

Itaku tracks likes / comments_count / reshares (NO views metric, and no
individual fave/comment rows), so get_ik_summary scopes the totals plus the
top_liked / top_reshared / fastest_growing lists. Verifies account_id filters to
one account and account_id=None aggregates across accounts (byte-identical to the
pre-scoping behaviour).
"""

import sqlite3

import pytest

import config
from database import accounts


@pytest.fixture
def conn():
    """Fresh DB with accounts + IK analytics tables cleared and settings reset."""
    from database.db import init_db, get_connection
    config.SETTINGS_PATH.write_text("{}", encoding="utf-8")
    init_db()
    c = get_connection()
    for t in ("personas", "accounts", "publications", "posting_queue",
              "posting_log", "ik_submissions", "ik_snapshots"):
        try:
            c.execute(f"DELETE FROM {t}")
        except sqlite3.OperationalError:
            pass
    c.commit()
    yield c
    c.close()


# ── Phase 2: per-account read scoping (Itaku) ──────────────────

def test_ik_summary_and_submissions_scope_by_account(conn):
    """get_ik_summary / get_all_ik_submissions filter to one account when given
    an account_id, and aggregate across accounts when not (account_id=None)."""
    from database import ik_queries
    a1 = accounts.create_account(conn, "ik", "ik-a1")
    a2 = accounts.create_account(conn, "ik", "ik-a2")
    # NB: upsert_ik_submission keys the row off sub["content_id"] (Itaku content id),
    # which populates the ik_submissions.submission_id PK.
    ik_queries.upsert_ik_submission(conn, {"content_id": 101, "title": "A1-one", "likes": 100, "comments_count": 1, "reshares": 2}, a1)
    ik_queries.upsert_ik_submission(conn, {"content_id": 102, "title": "A1-two", "likes": 50, "comments_count": 0, "reshares": 1}, a1)
    ik_queries.upsert_ik_submission(conn, {"content_id": 201, "title": "A2-one", "likes": 999, "comments_count": 3, "reshares": 9}, a2)
    conn.commit()

    s1 = ik_queries.get_ik_summary(conn, account_id=a1)
    assert s1["total_submissions"] == 2
    assert s1["total_likes"] == 150
    assert s1["total_reshares"] == 3
    assert [t["submission_id"] for t in s1["top_liked"]] == [101, 102]

    s2 = ik_queries.get_ik_summary(conn, account_id=a2)
    assert s2["total_submissions"] == 1 and s2["total_likes"] == 999

    s_all = ik_queries.get_ik_summary(conn)  # All accounts
    assert s_all["total_submissions"] == 3

    assert {x["submission_id"] for x in ik_queries.get_all_ik_submissions(conn, account_id=a1)} == {101, 102}
    assert {x["submission_id"] for x in ik_queries.get_all_ik_submissions(conn)} == {101, 102, 201}
