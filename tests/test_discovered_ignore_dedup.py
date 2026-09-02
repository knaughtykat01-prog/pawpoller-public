"""Discovered-hub filtering (2.140.0): dedup Masterpiece members + Ignore list.

get_discovered_unlinked must drop tiles that are (a) already a Masterpiece member
or (b) on the user's Ignore list, on top of the existing publication-linked filter.
"""
from database.db import get_connection
from database import ignored_queries, masterpiece_queries
from routes.submissions_api import get_discovered_unlinked


def _add_fa_submission(conn, sid, title="Art"):
    conn.execute(
        "INSERT OR REPLACE INTO fa_submissions (submission_id, title, username, account_id, "
        "category, thumbnail_url, posted_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (sid, title, "tester", 1, "Artwork (Digital)", "http://t/x.jpg", "2026-03-03"))
    conn.commit()


def _discovered_ids(conn):
    return {(d["platform"], d["submission_id"])
            for d in get_discovered_unlinked(conn, platform_filter="fa")}


def test_discovered_shows_unlinked():
    conn = get_connection()
    _add_fa_submission(conn, 555)
    assert ("fa", "555") in _discovered_ids(conn)
    conn.close()


def test_ignore_hides_and_unignore_restores():
    conn = get_connection()
    _add_fa_submission(conn, 556)
    assert ("fa", "556") in _discovered_ids(conn)
    ignored_queries.add_ignored(conn, "fa", "556")
    assert ("fa", "556") not in _discovered_ids(conn)      # ignored → hidden
    ignored_queries.remove_ignored(conn, "fa", "556")
    assert ("fa", "556") in _discovered_ids(conn)          # un-ignored → back
    conn.close()


def test_masterpiece_member_deduped():
    conn = get_connection()
    _add_fa_submission(conn, 557)
    assert ("fa", "557") in _discovered_ids(conn)
    masterpiece_queries.add_member(conn, "Some Masterpiece", "fa", "557")
    conn.commit()
    assert ("fa", "557") not in _discovered_ids(conn)      # a member is not a duplicate tile
    conn.close()
