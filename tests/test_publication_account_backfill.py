"""Regression: publications must be attributed to the account that owns the
source submission — not the platform default. Covers the 2.96.0 one-time
backfill migration and the `_submission_account_id` lookup used by imports/links.
"""
import sqlite3

from database.db import get_connection, _run_migrations
from database import posting_queries
from routes.submissions_api import _submission_account_id


def _seed_fa_submission(conn, sid, account_id):
    conn.execute(
        "INSERT INTO fa_submissions (submission_id, account_id, title) VALUES (?, ?, ?)",
        (sid, account_id, f"piece {sid}"))


def test_backfill_repoints_publication_account_from_submission():
    conn = get_connection()
    try:
        # Two FA submissions owned by DIFFERENT accounts (2 = the default,
        # 10 = a second persona), as polling correctly records them. FA ids are
        # numeric (INTEGER PK); external_id on publications is TEXT — the backfill
        # join must still match across the INTEGER↔TEXT affinity.
        _seed_fa_submission(conn, 1001, 2)
        _seed_fa_submission(conn, 1002, 10)
        # ...but both works were imported onto the DEFAULT account (the bug).
        posting_queries.upsert_publication(
            conn, story_name="W1", chapter_index=0, platform="fa",
            account_id=2, content_type="artwork", external_id="1001", status="posted")
        posting_queries.upsert_publication(
            conn, story_name="W2", chapter_index=0, platform="fa",
            account_id=2, content_type="artwork", external_id="1002", status="posted")
        conn.commit()

        # Re-arm the one-time backfill (init_db already ran it on the empty DB).
        conn.execute("DELETE FROM pp_meta WHERE key = 'pub_account_backfill_v1'")
        conn.commit()
        _run_migrations(conn)
        conn.commit()

        acc = {r["external_id"]: r["account_id"] for r in conn.execute(
            "SELECT external_id, account_id FROM publications WHERE platform='fa'")}
        assert acc["1001"] == 2    # already correct — unchanged
        assert acc["1002"] == 10   # re-pointed to the second persona's account

        # ...and the migration marks itself done (idempotent).
        assert conn.execute(
            "SELECT 1 FROM pp_meta WHERE key='pub_account_backfill_v1'").fetchone()
    finally:
        conn.close()


def test_submission_account_id_lookup():
    conn = get_connection()
    try:
        _seed_fa_submission(conn, 9009, 10)
        conn.commit()
        assert _submission_account_id(conn, "fa", "9009") == 10
        assert _submission_account_id(conn, "fa", "8888") is None
        assert _submission_account_id(conn, "notaplatform", "9009") is None
    finally:
        conn.close()
