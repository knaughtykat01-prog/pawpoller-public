"""Per-account read scoping test for the e621 platform.

e621 tracks score / favorites_count / comments_count (no view count); its
submission_id is the post number as TEXT. Asserts on totals + the scoped
submissions list, and that the E621Client parses a post correctly.
"""

import sqlite3

import pytest

import config
from database import accounts, e621_queries
from clients.e621.client import E621Client


@pytest.fixture
def conn():
    from database.db import init_db, get_connection
    config.SETTINGS_PATH.write_text("{}", encoding="utf-8")
    init_db()
    c = get_connection()
    for t in ("personas", "accounts", "e621_submissions", "e621_snapshots"):
        try:
            c.execute(f"DELETE FROM {t}")
        except sqlite3.OperationalError:
            pass
    c.commit()
    yield c
    c.close()


def test_e621_summary_and_submissions_scope_by_account(conn):
    a1 = accounts.create_account(conn, "e621", "e621-a1")
    a2 = accounts.create_account(conn, "e621", "e621-a2")
    e621_queries.upsert_e621_submission(conn, {"post_uri": "101", "title": "#101", "score": 120, "favorites_count": 100, "comments_count": 5}, a1)
    e621_queries.upsert_e621_submission(conn, {"post_uri": "102", "title": "#102", "score": 30, "favorites_count": 50, "comments_count": 2}, a1)
    e621_queries.upsert_e621_submission(conn, {"post_uri": "201", "title": "#201", "score": 900, "favorites_count": 900, "comments_count": 30}, a2)
    conn.commit()

    s1 = e621_queries.get_e621_summary(conn, account_id=a1)
    assert s1["total_submissions"] == 2
    assert s1["total_score"] == 150
    assert s1["total_favorites"] == 150
    assert [t["submission_id"] for t in s1["top_scored"]] == ["101", "102"]

    s2 = e621_queries.get_e621_summary(conn, account_id=a2)
    assert s2["total_submissions"] == 1 and s2["total_score"] == 900

    s_all = e621_queries.get_e621_summary(conn)  # All accounts
    assert s_all["total_submissions"] == 3

    assert {x["submission_id"] for x in e621_queries.get_all_e621_submissions(conn, account_id=a1)} == {"101", "102"}
    assert {x["submission_id"] for x in e621_queries.get_all_e621_submissions(conn)} == {"101", "102", "201"}


def test_e621_negative_score_supported(conn):
    """e621 score is up-minus-down and can be negative — it must round-trip."""
    a1 = accounts.create_account(conn, "e621", "e621-neg")
    e621_queries.upsert_e621_submission(conn, {"post_uri": "500", "title": "#500", "score": -12, "favorites_count": 3, "comments_count": 1}, a1)
    e621_queries.insert_e621_snapshot(conn, a1, "500", -12, 3, 1)
    conn.commit()
    sub = e621_queries.get_e621_submission(conn, "500")
    assert sub["score"] == -12
    snaps = e621_queries.get_e621_snapshots(conn, "500")
    assert snaps and snaps[-1]["score"] == -12


def test_e621_client_parse_post():
    """The e621 listing carries full engagement data; _parse_post maps it."""
    c = E621Client("tester", "key")
    post = {
        "id": 777,
        "created_at": "2026-01-01T00:00:00",
        "rating": "e",
        "file": {"url": "https://static1.e621.net/f.png", "ext": "png"},
        "preview": {"url": "https://static1.e621.net/p.jpg"},
        "score": {"up": 60, "down": -15, "total": 45},
        "fav_count": 12,
        "comment_count": 3,
        "tags": {"general": ["male", "solo"], "artist": ["someone"]},
        "description": "a caption\nsecond line",
    }
    parsed = c._parse_post(post)
    assert parsed["post_uri"] == "777"
    assert parsed["score"] == 45 and parsed["up_score"] == 60 and parsed["down_score"] == -15
    assert parsed["favorites_count"] == 12 and parsed["comments_count"] == 3
    assert parsed["rating"] == "Explicit"
    assert parsed["title"] == "a caption"  # first line of description
    assert parsed["content_type"] == "image"
    assert parsed["link"] == "https://e621.net/posts/777"
    assert "male" in parsed["keywords"] and "someone" in parsed["keywords"]


def test_e621_client_user_agent_is_descriptive_not_browser():
    """Policy: the UA must be descriptive and MUST NOT impersonate a browser."""
    c = E621Client("tester", "key")
    ua = c._headers()["User-Agent"]
    assert "PawPoller" in ua and "tester" in ua
    for browser_token in ("Mozilla", "Chrome", "Safari", "AppleWebKit", "Gecko"):
        assert browser_token not in ua
