"""The activity feed says what a poll actually saw change (POLLDELTA, FEEDACCT, FEEDWORDS).

Operator's screenshot, 2026-10-07: the bell listed X twice and FurAffinity three times, all
"no changes (N subs scanned)", beside "post Shimmering_Beauty". Three faults:

- "no changes" came from the poll log's new-faves / new-comments / new-watchers counters,
  which most sites never fill — X's log has none at all — so views and likes moving on every
  site still read as "no changes". The snapshots every poller writes are compared instead.
- The rows were one per ACCOUNT, but nothing said which, so they looked like duplicates.
- A post row printed the raw action and the folder name.
"""
from __future__ import annotations

import json

import pytest

from database import platform_metrics
from routes import api as api_routes


def _conn():
    from database.db import get_connection
    c = get_connection()
    c.execute("PRAGMA foreign_keys = OFF")
    return c


def _tw_snap(conn, account_id, sub, at, views, likes, replies=0):
    conn.execute(
        "INSERT INTO tw_snapshots (account_id, submission_id, polled_at, views, likes, retweets,"
        " replies, quotes, bookmarks) VALUES (?, ?, ?, ?, ?, 0, ?, 0, 0)",
        (account_id, sub, at, views, likes, replies))


def _tw_poll(conn, account_id, start, end, subs):
    conn.execute(
        "INSERT INTO tw_poll_log (started_at, finished_at, status, submissions_found, account_id)"
        " VALUES (?, ?, 'success', ?, ?)", (start, end, subs, account_id))


def test_a_poll_reports_the_views_and_likes_it_saw_move():
    conn = _conn()
    # Previous poll, then this one: post 1 gained 120 views + 3 likes, post 2 gained 5 views.
    _tw_snap(conn, 1, "t1", "2026-10-07 01:00:05", 1000, 10)
    _tw_snap(conn, 1, "t2", "2026-10-07 01:00:05", 50, 2)
    _tw_snap(conn, 1, "t1", "2026-10-07 02:00:05", 1120, 13, replies=1)
    _tw_snap(conn, 1, "t2", "2026-10-07 02:00:05", 55, 2)
    # A post first seen in this poll has nothing to compare with — left out.
    _tw_snap(conn, 1, "t3", "2026-10-07 02:00:06", 9999, 99)
    # Another account's posts in the same window don't count for this one.
    _tw_snap(conn, 2, "t9", "2026-10-07 01:00:05", 0, 0)
    _tw_snap(conn, 2, "t9", "2026-10-07 02:00:05", 500, 50)
    conn.commit()

    d = platform_metrics.poll_deltas(conn, "tw", "2026-10-07 02:00:00", "2026-10-07 02:01:00", 1)
    assert d == {"views": 125, "faves": 3, "comments": 1, "posts": 2}
    summary = api_routes._format_poll_summary({"status": "success", "submissions_found": 3}, "tw", d)
    assert summary == "+125 views, +3 likes, +1 reply"


def test_nothing_moved_says_so_in_words_and_is_quiet():
    d = {"views": 0, "faves": 0, "comments": 0, "posts": 22}
    assert api_routes._format_poll_summary(
        {"status": "success", "submissions_found": 22}, "tw", d) == "nothing new · 22 posts checked"
    assert api_routes._format_poll_summary(
        {"status": "success", "submissions_found": 1}, "fa", d) == "nothing new · 1 post checked"
    assert api_routes._format_poll_summary({"status": "success"}, "fa", None) == "no posts found"


def test_site_words_and_score():
    assert api_routes._format_poll_summary({"status": "success"}, "tum", {"faves": 4}) == "+4 notes"
    assert api_routes._format_poll_summary({"status": "success"}, "e621", {"score": -2, "faves": 1}) \
        == "score -2, +1 fave"
    # Watchers live in the log, not the snapshots, and still show.
    assert api_routes._format_poll_summary(
        {"status": "success", "new_watchers_found": 2}, "fa", {"views": 7}) == "+7 views, +2 watchers"


@pytest.mark.parametrize("code", platform_metrics.ALL_CODES)
def test_every_sites_snapshots_can_be_compared(code):
    """A table without `id` / `account_id` / `polled_at` would silently fall back to the old
    counters; this pins every registered site to the real schema."""
    conn = _conn()
    assert platform_metrics.poll_deltas(conn, code, "2026-10-07 02:00:00", "2026-10-07 02:01:00", 1) \
        is not None, code


def test_the_feed_names_the_account_only_when_a_site_has_several(monkeypatch):
    conn = _conn()
    conn.execute("INSERT INTO accounts (account_id, platform, label, handle, is_default) VALUES (1, 'tw', 'Me', 'FirstFur', 1)")
    conn.execute("INSERT INTO accounts (account_id, platform, label, handle, is_default) VALUES (2, 'tw', 'Brand', 'PawPoller', 0)")
    conn.execute("INSERT INTO accounts (account_id, platform, label, handle, is_default) VALUES (3, 'bsky', 'Me', 'me.bsky.social', 1)")
    _tw_snap(conn, 1, "t1", "2026-10-07 01:00:05", 10, 1)
    _tw_snap(conn, 1, "t1", "2026-10-07 02:00:05", 10, 1)
    _tw_poll(conn, 1, "2026-10-07 02:00:00", "2026-10-07 02:01:00", 1)
    _tw_poll(conn, 2, "2026-10-07 02:00:00", "2026-10-07 02:01:00", 0)
    conn.commit()
    conn.close()

    events = [e for e in api_routes._collect_activity_events(30) if e["platform"] == "tw"]
    by_acct = {e["account"]: e for e in events}
    assert set(by_acct) == {"FirstFur", "PawPoller"}
    assert by_acct["FirstFur"]["summary"] == "nothing new · 1 post checked"
    assert by_acct["FirstFur"]["quiet"] is True
    assert by_acct["PawPoller"]["summary"] == "no posts found"


def test_a_post_row_uses_the_pieces_title(tmp_path, monkeypatch):
    from posting import artwork_reader
    monkeypatch.setattr(artwork_reader, "get_artwork_archive_path", lambda: tmp_path)
    (tmp_path / "Shimmering_Beauty").mkdir()
    (tmp_path / "Shimmering_Beauty" / "masterpiece.json").write_text(
        json.dumps({"title": "Shimmering Beauty"}), encoding="utf-8")
    conn = _conn()
    conn.execute(
        "INSERT INTO posting_log (platform, story_name, chapter_index, account_id, content_type, action, status)"
        " VALUES ('bsky', 'Shimmering_Beauty', 0, 3, 'artwork', 'post', 'success')")
    conn.execute(
        "INSERT INTO posting_log (platform, story_name, chapter_index, account_id, content_type, action, status)"
        " VALUES ('fa', 'Gone_Folder', 2, 0, 'story', 'post', 'error')")
    conn.commit()
    conn.close()
    summaries = {e["platform"]: e["summary"] for e in api_routes._collect_activity_events(30)
                 if e["kind"] == "post"}
    assert summaries["bsky"] == "Posted Shimmering Beauty"
    # No metadata to read: the folder name, de-underscored, and the chapter in words.
    assert summaries["fa"] == "Couldn't post Gone Folder (chapter 2)"
