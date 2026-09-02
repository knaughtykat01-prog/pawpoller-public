"""Unified comment inbox (gap G3).

Covers the storage layer (dedupe, delta count, handled flags), the cross-source
union query (IB legacy table + platform_comments, permalink construction,
newest-first order), and the API surface (feed + handled toggle + reply
validation). Native-reply network paths are exercised at validation level only
— the platform clients are covered by their own modules.
"""
from fastapi import FastAPI
from fastapi.testclient import TestClient

from database.db import get_connection
from database import inbox_queries
from routes.inbox_api import inbox_router


def _client():
    app = FastAPI()
    app.include_router(inbox_router)
    return TestClient(app)


def _seed_platform_comment(conn, cid="at://did:plc:x/app.bsky.feed.post/r1", **kw):
    base = dict(author="fan1", body="great piece!", commented_at="2026-07-20T10:00:00Z",
                permalink="https://bsky.app/profile/fan1/post/r1",
                submission_title="My Post",
                meta={"cid": "c1", "root_uri": "at://root", "root_cid": "rc1"})
    base.update(kw)
    return inbox_queries.upsert_platform_comment(
        conn, "bsky", cid, "at://did:plc:me/app.bsky.feed.post/p1", **base)


def test_upsert_dedupes_and_counts():
    conn = get_connection()
    try:
        assert _seed_platform_comment(conn) is True
        assert _seed_platform_comment(conn) is False      # same id → ignored
        assert inbox_queries.count_for_submission(
            conn, "bsky", "at://did:plc:me/app.bsky.feed.post/p1") == 1
    finally:
        conn.close()


def test_union_includes_ib_and_constructs_permalink():
    conn = get_connection()
    try:
        # IB legacy comments table (created by init_db). FKs are enforced, so
        # the parent submissions row comes first (all display columns default).
        conn.execute(
            "INSERT INTO submissions (submission_id, title) VALUES (555, 'My IB Piece')")
        conn.execute(
            "INSERT INTO comments (comment_id, submission_id, username, comment_text,"
            " commented_at, first_seen_at) VALUES (1001, 555, 'ibfan', 'lovely!',"
            " '2026-07-19', '2026-07-19 09:00:00')")
        conn.commit()
        _seed_platform_comment(conn)

        items = inbox_queries.get_inbox(conn)
    finally:
        conn.close()
    plats = {i["platform"] for i in items}
    assert {"ib", "bsky"} <= plats
    ib = next(i for i in items if i["platform"] == "ib")
    assert ib["permalink"] == "https://inkbunny.net/s/555#commentid_1001"
    assert ib["can_reply"] is False                       # IB = reply on-site
    bsky = next(i for i in items if i["platform"] == "bsky")
    assert bsky["can_reply"] is True
    assert bsky["meta"]["root_uri"] == "at://root"        # reply refs survive


def test_handled_flag_flows_and_filters():
    conn = get_connection()
    try:
        _seed_platform_comment(conn)
        cid = "at://did:plc:x/app.bsky.feed.post/r1"
        inbox_queries.set_handled(conn, "bsky", cid, True)
        assert all(i["handled"] for i in inbox_queries.get_inbox(conn))
        assert inbox_queries.get_inbox(conn, unhandled_only=True) == []
        inbox_queries.set_handled(conn, "bsky", cid, False)
        assert len(inbox_queries.get_inbox(conn, unhandled_only=True)) == 1
    finally:
        conn.close()


def test_api_feed_and_handled_toggle():
    conn = get_connection()
    try:
        _seed_platform_comment(conn)
    finally:
        conn.close()
    c = _client()
    r = c.get("/api/inbox")
    assert r.status_code == 200
    data = r.json()
    assert data["unhandled_count"] == 1
    assert data["items"][0]["author"] == "fan1"

    cid = data["items"][0]["comment_id"]
    assert c.post("/api/inbox/handled",
                  json={"platform": "bsky", "comment_id": cid}).status_code == 200
    assert c.get("/api/inbox").json()["unhandled_count"] == 0


def test_reply_validation():
    c = _client()
    # Unsupported platform → clear 400, not a crash.
    r = c.post("/api/inbox/reply",
               json={"platform": "fa", "comment_id": "x", "text": "hi"})
    assert r.status_code == 400
    assert "on-site" in r.json()["detail"]
    # Unknown comment on a replyable platform → 404.
    r = c.post("/api/inbox/reply",
               json={"platform": "bsky", "comment_id": "nope", "text": "hi"})
    assert r.status_code == 404
    # Missing text → 400.
    r = c.post("/api/inbox/reply", json={"platform": "bsky", "comment_id": "x"})
    assert r.status_code == 400
