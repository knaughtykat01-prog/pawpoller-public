"""Overnight (spec 026): what happened while you were away.

Gains are read from the snapshots every poller writes (value at a moment = the latest snapshot in the day
before it), pooled per Library piece, with e621-style score kept apart from views.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from polling import overnight

NOW = datetime(2026, 10, 10, 7, 0, tzinfo=timezone.utc)
START = NOW - timedelta(hours=8)


def _conn():
    from database.db import get_connection
    c = get_connection()
    c.execute("PRAGMA foreign_keys = OFF")
    return c


def _ts(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _tw(conn, sid, at, views, likes, replies=0, title=""):
    conn.execute("INSERT OR IGNORE INTO tw_submissions (submission_id, title) VALUES (?, ?)", (sid, title or sid))
    conn.execute("INSERT INTO tw_snapshots (account_id, submission_id, polled_at, views, likes, retweets, replies,"
                 " quotes, bookmarks) VALUES (1, ?, ?, ?, ?, 0, ?, 0, 0)", (sid, _ts(at), views, likes, replies))


def _e621(conn, sid, at, score, faves):
    conn.execute("INSERT OR IGNORE INTO e621_submissions (submission_id) VALUES (?)", (sid,))
    conn.execute("INSERT INTO e621_snapshots (submission_id, polled_at, score, favorites_count, comments_count)"
                 " VALUES (?, ?, ?, ?, 0)", (sid, _ts(at), score, faves))


def test_a_post_gains_what_moved_between_the_window_ends():
    conn = _conn()
    _tw(conn, "t1", START - timedelta(hours=1), 1000, 10)
    _tw(conn, "t1", START + timedelta(hours=4), 1300, 12)
    _tw(conn, "t1", NOW - timedelta(minutes=5), 1600, 15, replies=2)
    _tw(conn, "t2", NOW - timedelta(hours=2), 9999, 99)          # first seen inside the window: not a gain
    conn.commit()
    ch, had = overnight.changes(conn, "tw", START, NOW)
    assert had
    assert ch["t1"]["gain"] == {"views": 600, "faves": 5, "comments": 2}
    assert "t2" not in ch


def test_score_is_never_counted_as_views():
    conn = _conn()
    _tw(conn, "t1", START - timedelta(hours=1), 100, 0)
    _tw(conn, "t1", NOW - timedelta(minutes=1), 150, 0)
    _e621(conn, "e1", START - timedelta(hours=1), 10, 1)
    _e621(conn, "e1", NOW - timedelta(minutes=1), 40, 4)
    conn.commit()
    t = overnight.totals(conn, START, NOW)["totals"]
    assert t["views"] == 50 and t["score"] == 30 and t["faves"] == 3


def test_a_snapshot_older_than_a_day_is_not_a_starting_value():
    conn = _conn()
    _tw(conn, "t1", START - timedelta(hours=30), 0, 0)
    _tw(conn, "t1", NOW - timedelta(minutes=1), 500, 5)
    conn.commit()
    assert overnight.changes(conn, "tw", START, NOW) == ({}, False)


def test_pieces_pool_one_library_piece_across_its_uploads():
    conn = _conn()
    _tw(conn, "t1", START - timedelta(hours=1), 0, 0)
    _tw(conn, "t1", NOW - timedelta(minutes=1), 400, 4)
    _e621(conn, "e1", START - timedelta(hours=1), 0, 0)
    _e621(conn, "e1", NOW - timedelta(minutes=1), 20, 2)
    _tw(conn, "t9", START - timedelta(hours=1), 0, 0, title="A lone post")
    _tw(conn, "t9", NOW - timedelta(minutes=1), 10, 0)
    for plat, sid in (("tw", "t1"), ("e621", "e1")):
        conn.execute("INSERT INTO masterpiece_members (masterpiece_name, platform, submission_id) VALUES (?, ?, ?)",
                     ("Sample_Piece", plat, sid))
    conn.commit()
    ch = overnight.totals(conn, START, NOW, keep_changes=True)["changes"]
    pieces = overnight.top_pieces(conn, ch, START, NOW)
    assert pieces[0]["title"] == "Sample Piece" and pieces[0]["href"] == "#/masterpieces/Sample_Piece"
    assert pieces[0]["gain"]["views"] == 400 and pieces[0]["gain"]["score"] == 20
    assert {s["platform"]: s["gain"] for s in pieces[0]["sites"]} == {"tw": 400, "e621": 20}
    assert pieces[0]["series"][-1] == 420 and pieces[0]["series"][0] == 0
    assert pieces[1]["title"] == "A lone post" and pieces[1]["kind"] == "post"


def test_a_story_names_its_busiest_chapter():
    conn = _conn()
    for sid, gain in (("c1", 5), ("c4", 80)):
        _tw(conn, sid, START - timedelta(hours=1), 0, 0)
        _tw(conn, sid, NOW - timedelta(minutes=1), gain, 0)
    conn.execute("INSERT INTO publications (story_name, chapter_index, platform, external_id, content_type)"
                 " VALUES ('Sample_Story', 0, 'tw', 'c1', 'story'), ('Sample_Story', 3, 'tw', 'c4', 'story')")
    conn.commit()
    ch = overnight.totals(conn, START, NOW, keep_changes=True)["changes"]
    p = overnight.top_pieces(conn, ch, START, NOW)[0]
    assert p["title"] == "Sample Story" and p["chapter"] == 4 and p["gain"]["views"] == 85


def test_a_crossed_milestone_is_named():
    conn = _conn()
    _tw(conn, "t1", START - timedelta(hours=1), 900, 0)
    _tw(conn, "t1", NOW - timedelta(minutes=1), 1100, 0)
    conn.commit()
    ch = overnight.totals(conn, START, NOW, keep_changes=True)["changes"]
    assert overnight.top_pieces(conn, ch, START, NOW)[0]["milestones"] == [
        {"platform": "tw", "metric": "views", "value": 1000}]


def test_usual_needs_three_earlier_nights_and_averages_them():
    conn = _conn()
    for k in (1, 2):
        _tw(conn, "t1", START - timedelta(days=k, hours=1), 0, 0)
        _tw(conn, "t1", NOW - timedelta(days=k, minutes=1), 100, 0)
    conn.commit()
    assert overnight.usual(conn, START, NOW) is None
    _tw(conn, "t2", START - timedelta(days=3, hours=1), 0, 0)
    _tw(conn, "t2", NOW - timedelta(days=3, minutes=1), 400, 0)
    conn.commit()
    u = overnight.usual(conn, START, NOW)
    assert u["views"] == 200.0          # (100 + 100 + 400) / 3
    assert overnight.usual(conn, NOW - timedelta(days=2), NOW) is None   # over a day: no like-for-like


def test_named_followers_and_comments_in_the_window():
    conn = _conn()
    conn.execute("INSERT INTO fa_watchers (account_id, username, first_seen_at, confirmed, is_spam)"
                 " VALUES (1, 'SecondFur', ?, 1, 0), (1, 'Spammer', ?, 1, 1), (1, 'OldFriend', ?, 1, 0)",
                 (_ts(NOW - timedelta(hours=1)), _ts(NOW - timedelta(hours=1)), _ts(START - timedelta(days=2))))
    conn.commit()
    t = overnight.totals(conn, START, NOW)
    assert [f["name"] for f in t["named_followers"]] == ["SecondFur"]
    assert t["totals"]["followers"] == 1


def test_did_lists_a_failure_first_and_a_later_success_clears_it():
    conn = _conn()
    conn.execute("INSERT INTO posting_log (platform, story_name, action, status, error_message, created_at) VALUES"
                 " ('fa', 'Sample_Piece', 'post', 'error', 'FurAffinity signed you out', ?),"
                 " ('ws', 'Third_Piece', 'post', 'error', 'boom', ?),"
                 " ('ws', 'Third_Piece', 'post', 'success', '', ?),"
                 " ('ib', 'Sample_Story', 'post', 'success', '', ?)",
                 (_ts(NOW - timedelta(hours=5)), _ts(NOW - timedelta(hours=4)), _ts(NOW - timedelta(hours=3)),
                  _ts(NOW - timedelta(hours=2))))
    conn.commit()
    d = overnight.did(conn, START, NOW)
    posts = [a for a in d["attention"] if a["kind"] == "post"]
    assert [(a["title"], a["platform"]) for a in posts] == [("Sample Piece", "fa")]
    assert {p["title"] for p in d["posted"]} == {"Third Piece", "Sample Story"}


@pytest.mark.parametrize("show_after,away_h,expect", [
    ("6", 7, True), ("6", 5, False), ("8", 7, False), ("12", 13, True), ("never", 100, False)])
def test_due_after_hours_away(show_after, away_h, expect):
    assert overnight.due(NOW - timedelta(hours=away_h), show_after, NOW, "UTC") is expect


def test_due_every_morning_and_never_on_a_first_visit():
    morning = datetime(2026, 10, 10, 6, 0, tzinfo=timezone.utc)
    assert overnight.due(morning - timedelta(hours=3), "morning", morning, "UTC")          # last seen yesterday
    assert not overnight.due(morning - timedelta(minutes=30), "morning", morning, "UTC")   # already seen today
    assert not overnight.due(morning - timedelta(hours=3), "morning", morning.replace(hour=4), "UTC")
    assert not overnight.due(None, "6", NOW, "UTC")


def test_the_api_opens_once_then_notes_the_visit(monkeypatch):
    import config
    from routes import overnight_api
    store = {"overnight_last_seen_at": (datetime.now(timezone.utc) - timedelta(hours=9)).isoformat()}
    monkeypatch.setattr(config, "get_settings", lambda: dict(store))
    monkeypatch.setattr(config, "save_settings", lambda u: store.update(u))
    monkeypatch.setattr(overnight, "build", lambda conn, s, e, z=None: {"has_news": True, "totals": {}})
    assert overnight_api.auto()["show"] is True
    overnight_api.seen({})
    assert overnight_api.auto() == {"show": False}
    overnight_api.seen({"show_after": "morning"})
    assert store["overnight_show_after"] == "morning"
    with pytest.raises(Exception):
        overnight_api.seen({"show_after": "<script>"})


def test_nothing_happened_means_no_sheet(monkeypatch):
    import config
    from routes import overnight_api
    store = {"overnight_last_seen_at": (datetime.now(timezone.utc) - timedelta(hours=9)).isoformat()}
    monkeypatch.setattr(config, "get_settings", lambda: dict(store))
    monkeypatch.setattr(config, "save_settings", lambda u: store.update(u))
    monkeypatch.setattr(overnight, "build", lambda conn, s, e, z=None: {"has_news": False})
    assert overnight_api.auto() == {"show": False}
    assert overnight.parse_ts(store["overnight_last_seen_at"]) > datetime.now(timezone.utc) - timedelta(minutes=1)


def test_the_sheet_escapes_other_peoples_words():
    src = open("frontend/js/overnight.js", encoding="utf-8").read()
    for field in ("c.author", "c.body", "c.title", "f.name"):
        assert f"this._esc({field})" in src, field


def test_comments_are_the_unhandled_ones_first_seen_in_the_window():
    from database import inbox_queries
    conn = _conn()
    rows = [("bsky", "c1", "SecondFur", "lovely", NOW - timedelta(hours=1), 0),
            ("bsky", "c2", "Owner", "thanks!", NOW - timedelta(hours=1), 1),        # our own: handled
            ("bsky", "c3", "ThirdFur", "old one", START - timedelta(days=1), 0)]
    for plat, cid, who, body, at, own in rows:
        conn.execute("INSERT INTO platform_comments (platform, comment_id, submission_id, author, body, first_seen_at,"
                     " submission_title, is_own) VALUES (?, ?, 's1', ?, ?, ?, 'Sample Piece', ?)",
                     (plat, cid, who, body, _ts(at), own))
    conn.commit()
    c = overnight.comments(conn, START, NOW)
    assert c["count"] == 1 and c["items"][0]["author"] == "SecondFur" and c["items"][0]["title"] == "Sample Piece"
