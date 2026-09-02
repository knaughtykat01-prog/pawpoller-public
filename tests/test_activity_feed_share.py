"""The activity feed has to show posting, not just polling (3.17.0).

Two separate reasons a post you just made was missing from "Recent System
Events", both fixed here:

  1. **Crowding.** A poll CYCLE fires all 11 platforms at once, so it lands ~11
     rows sharing a timestamp. Merge everything, sort by time, take the newest
     30, and two cycles fill the feed outright. Posting now keeps a reserved
     share instead of competing on timestamp alone.

  2. **Silence.** A post refused by `poster.validate()` never reached
     `log_posting_action` at all — it `continue`d straight past it. So a
     rejected platform looked identical to one that was never selected. That is
     how the DeviantArt tag bug hid: ten platforms logged `success` for the
     piece and DA logged nothing.
"""
from __future__ import annotations

import pytest

import routes.api as api


def _poll(platform: str, ts: str) -> dict:
    return {"timestamp": ts, "platform": platform, "kind": "poll",
            "status": "success", "summary": f"{platform} poll", "detail": None}


def _post(platform: str, ts: str) -> dict:
    return {"timestamp": ts, "platform": platform, "kind": "post",
            "status": "success", "summary": f"post {platform}", "detail": None}


def _merge(polls, posts, limit):
    """Run the real collector with its two DB reads stubbed.

    Faithful to the shape that caused the bug: the poll stream is spread over
    ELEVEN platform entries, because `_collect_activity_events` fetches
    `limit // 4` rows *per platform* and a real cycle writes one row to each.
    Collapsing that into a single fake platform would quietly shrink the poll
    pool to 7 and the crowding this test is about would not occur.
    """
    class _FakeConn:
        def close(self):
            pass

    codes = ["ib", "fa", "ws", "sf", "da", "ik", "bsky", "e621", "fn", "fbr", "tw"]
    chunks = {c: polls[i::len(codes)] for i, c in enumerate(codes)}

    def _module_for(code):
        class _Q:
            @staticmethod
            def get_poll_log(conn, n):
                return [{"started_at": e["timestamp"], "status": "success",
                         "items_found": 0, "items_new": 0}
                        for e in chunks[code][:n]]
        return _Q

    import database.posting_queries as pq
    saved_conn = api.get_connection
    saved_cfg = api._PLATFORM_HEALTH_CONFIG
    saved_log = pq.get_posting_log

    api.get_connection = lambda: _FakeConn()          # type: ignore[assignment]
    api._PLATFORM_HEALTH_CONFIG = [
        (c, _module_for(c), None, 0, True) for c in codes]
    # The real query returns newest-first and THEN applies its limit; a stub
    # that slices the list as given would hand back the oldest rows instead.
    pq.get_posting_log = lambda conn, story_name=None, limit=0, content_type=None: [
        {"created_at": e["timestamp"], "platform": e["platform"],
         "action": "post", "status": "success", "story_name": "W",
         "chapter_index": 0, "error_message": None}
        for e in sorted(posts, key=lambda x: x["timestamp"], reverse=True)[:limit]]
    try:
        return api._collect_activity_events(limit)
    finally:
        api.get_connection = saved_conn                # type: ignore[assignment]
        api._PLATFORM_HEALTH_CONFIG = saved_cfg
        pq.get_posting_log = saved_log


# ── crowding ─────────────────────────────────────────────────────

def test_a_post_survives_a_wall_of_newer_poll_events():
    """The reported symptom. Polls are newer here, and under a pure timestamp
    merge they would take all 30 rows."""
    polls = [_poll("ib", f"2026-08-20T2{i // 10}:{i % 10:02d}:00") for i in range(40)]
    posts = [_post("da", "2026-08-20T13:35:00")]
    out = _merge(polls, posts, 30)
    assert any(e["kind"] == "post" for e in out), (
        "a post must not be crowded out by a poll cycle")


def test_the_reserved_share_is_a_third_of_the_feed():
    polls = [_poll("ib", f"2026-08-20T23:{i:02d}:00") for i in range(40)]
    posts = [_post("da", f"2026-08-20T13:{i:02d}:00") for i in range(20)]
    out = _merge(polls, posts, 30)
    assert sum(1 for e in out if e["kind"] == "post") >= 10


def test_polls_still_take_the_whole_feed_when_nothing_was_posted():
    """The reservation must not invent empty slots."""
    polls = [_poll("ib", f"2026-08-20T23:{i:02d}:00") for i in range(40)]
    out = _merge(polls, [], 30)
    assert out and all(e["kind"] == "poll" for e in out)


def test_the_feed_never_exceeds_the_requested_limit():
    polls = [_poll("ib", f"2026-08-20T23:{i:02d}:00") for i in range(40)]
    posts = [_post("da", f"2026-08-20T22:{i:02d}:00") for i in range(40)]
    assert len(_merge(polls, posts, 30)) == 30


def test_the_feed_is_still_in_newest_first_order():
    """Seating posts first must not leave them out of order in the output."""
    polls = [_poll("ib", f"2026-08-20T23:{i:02d}:00") for i in range(20)]
    posts = [_post("da", f"2026-08-20T12:{i:02d}:00") for i in range(5)]
    stamps = [e["timestamp"] for e in _merge(polls, posts, 30)]
    assert stamps == sorted(stamps, reverse=True)


def test_the_newest_posts_are_the_ones_kept():
    posts = [_post("da", f"2026-08-20T1{i}:00:00") for i in range(5)]
    out = _merge([], posts, 3)
    assert [e["timestamp"] for e in out] == [
        "2026-08-20T14:00:00", "2026-08-20T13:00:00", "2026-08-20T12:00:00"]


# ── silence ──────────────────────────────────────────────────────

def test_a_validation_failure_is_written_to_the_posting_log(db_conn, monkeypatch):
    """Refused-before-sending must leave a trace. Without this, a rejected
    platform is indistinguishable from one that was never attempted."""
    from posting import manager
    from database import posting_queries

    class _KeepOpen:
        """`sqlite3.Connection.close` is read-only, and the helper closes what
        it opens; the test needs the connection afterwards to read the row."""
        def __init__(self, c): self._c = c
        def __getattr__(self, n): return getattr(self._c, n)
        def close(self): pass

    monkeypatch.setattr(manager, "get_connection", lambda: _KeepOpen(db_conn))
    manager._log_validation_failure(
        "da", "Some_Work", 0, ["DA max 30 tags (got 50)"],
        account_id=0, content_type="artwork")

    rows = posting_queries.get_posting_log(db_conn, story_name="Some_Work", limit=5,
                                           content_type="artwork")
    assert len(rows) == 1
    assert rows[0]["status"] == "failed"
    assert "Rejected before sending" in rows[0]["error_message"]
    assert "30 tags" in rows[0]["error_message"]


def test_logging_a_validation_failure_never_raises(monkeypatch):
    """Bookkeeping must not be able to sink a posting run."""
    from posting import manager

    def _boom():
        raise RuntimeError("db gone")
    monkeypatch.setattr(manager, "get_connection", _boom)
    manager._log_validation_failure("da", "W", 0, ["x"])      # must not raise


def test_artwork_posts_appear_in_the_feed_at_all(db_conn, monkeypatch):
    """`get_posting_log` defaults to content_type="story" so the Stories log
    view never shows artwork. The activity feed is cross-cutting and inherited
    that default, so every artwork post was filtered out before the merge even
    ran — nine successful uploads, zero rows in the timeline.

    Pinned against the real query rather than the stub, because the bug was in
    the ARGUMENT the feed passes, and a stub that ignores `content_type` would
    report success either way.
    """
    from database import posting_queries

    posting_queries.log_posting_action(
        db_conn, "bsky", "Some_Art", 0, action="post", status="success",
        content_type="artwork")
    posting_queries.log_posting_action(
        db_conn, "ao3", "Some_Story", 1, action="post", status="success",
        content_type="story")

    both = posting_queries.get_posting_log(db_conn, story_name=None, limit=30,
                                           content_type=None)
    names = {r["story_name"] for r in both}
    assert names == {"Some_Art", "Some_Story"}, (
        "the feed must pass content_type=None or artwork never appears")

    story_only = posting_queries.get_posting_log(db_conn, story_name=None, limit=30)
    assert {r["story_name"] for r in story_only} == {"Some_Story"}, (
        "the default is still story-only — that is what the Stories view wants")
