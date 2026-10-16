"""Spec 043: replies to the account's X posts reach the Inbox, and can be answered from it."""
import asyncio
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from clients.tw import gallerydl
from database import inbox_queries
from database.db import get_connection
from polling import tw_poller

FOCAL = "2000000000000000001"


def _row(tid, reply_id, name="fan1", text="love this", media=False):
    kw = {"tweet_id": int(tid), "reply_id": int(reply_id or 0), "content": text,
          "date": "2026-10-15 20:03:45", "author": {"name": name, "nick": name.title()}}
    return [3, "https://pbs.twimg.com/media/a.jpg", kw] if media else [2, kw]


def _dump(*rows):
    return json.dumps(list(rows))


def test_only_direct_replies_once_each_in_the_inbox_shape():
    raw = _dump(_row(FOCAL, 0, "SecondFur", "the post"),                 # the post itself
                _row("2000000000000000002", FOCAL, media=True),
                _row("2000000000000000002", FOCAL, media=True),          # 2nd image, same reply
                _row("2000000000000000003", "2000000000000000002"),      # a reply to a reply
                _row("2000000000000000004", FOCAL, "Inkwolf", "nice"))
    got = gallerydl._parse_replies(raw, FOCAL)
    assert [r["comment_id"] for r in got] == ["2000000000000000002", "2000000000000000004"]
    assert got[1] == {"comment_id": "2000000000000000004", "author": "Inkwolf", "body": "nice",
                      "commented_at": got[1]["commented_at"],
                      "permalink": "https://x.com/Inkwolf/status/2000000000000000004",
                      "meta": {"nick": "Inkwolf"}}
    assert got[1]["commented_at"].startswith("2026-10-15")


def test_a_refusal_dump_is_not_no_replies():
    """gallery-dl exits 0 with {"error": "AuthRequired"} in the dump."""
    with pytest.raises(ValueError):
        gallerydl._parse_replies(_dump([-1, {"error": "AuthRequired", "message": "cookies needed"}]), FOCAL)
    assert gallerydl._parse_replies("not json", FOCAL) == []


def test_fetch_replies_reads_the_thread_and_reports_failure_as_none(monkeypatch):
    calls = []

    async def _run(exe, url, cookie_path, settings, **kw):
        calls.append((url, kw))
        return 0, _dump(_row("2000000000000000002", FOCAL)), ""

    monkeypatch.setattr(gallerydl, "is_enabled", lambda s=None: True)
    monkeypatch.setattr(gallerydl, "find_gallerydl", lambda s=None: "gallery-dl")
    monkeypatch.setattr(gallerydl, "_run", _run)
    got = asyncio.run(gallerydl.fetch_replies("tok", "ct0", "@SecondFur", FOCAL, {}))
    assert [r["comment_id"] for r in got] == ["2000000000000000002"]
    url, kw = calls[0]
    assert url == f"https://x.com/SecondFur/status/{FOCAL}"
    assert "extractor.twitter.conversations=true" in kw["extra"] and kw["range_spec"] and kw["timeout"]

    async def _refused(*a, **k):
        return 0, _dump([-1, {"error": "AuthRequired"}]), ""

    monkeypatch.setattr(gallerydl, "_run", _refused)
    assert asyncio.run(gallerydl.fetch_replies("tok", "ct0", "SecondFur", FOCAL, {})) is None
    assert asyncio.run(gallerydl.fetch_replies("tok", "ct0", "SecondFur", "../x", {})) is None
    assert asyncio.run(gallerydl.fetch_replies("", "ct0", "SecondFur", FOCAL, {})) is None


class _Client:
    auth_token, ct0, target_user, throttled = "tok", "ct0", "SecondFur", False


def _details(n):
    return [{"tweet_id": str(3000000000000000000 + i), "replies": 1, "content_type": "tweet",
             "title": f"Sample {i}"} for i in range(n)]


def test_the_poll_captures_replies_capped_and_once_per_count(monkeypatch):
    tw_poller._REPLIES_SEEN.clear()
    fetched = []

    async def _fetch(tok, ct0, user, tid, settings=None):
        fetched.append(tid)
        return [{"comment_id": str(int(tid) + 1), "author": "Inkwolf", "body": "hi",
                 "commented_at": "2026-10-15T20:03:45Z", "permalink": "https://x.com/Inkwolf/status/1"}]

    monkeypatch.setattr(gallerydl, "is_enabled", lambda s=None: True)
    monkeypatch.setattr(gallerydl, "fetch_replies", _fetch)
    details = _details(7)
    conn = get_connection()
    try:
        asyncio.run(tw_poller._capture_replies(conn, _Client(), details, 1, {}))
        assert len(fetched) == tw_poller._REPLY_CAP == 5                      # newest first, capped
        assert inbox_queries.count_for_submission(conn, "tw", details[0]["tweet_id"]) == 1
        asyncio.run(tw_poller._capture_replies(conn, _Client(), details, 1, {}))
        assert len(fetched) == 7                                              # the other two next cycle
        asyncio.run(tw_poller._capture_replies(conn, _Client(), details, 1, {}))
        assert len(fetched) == 7                                              # caught up: no reads
    finally:
        conn.close()
    item = next(i for i in inbox_queries.get_inbox(get_connection(), platform="tw") if i["author"] == "Inkwolf")
    assert item["can_reply"] and not item["handled"]


def test_a_count_x_never_shows_is_read_once_not_every_cycle(monkeypatch):
    tw_poller._REPLIES_SEEN.clear()
    fetched = []

    async def _fetch(*a, **k):
        fetched.append(a[3])
        return []                                    # X counts a reply it won't show

    monkeypatch.setattr(gallerydl, "is_enabled", lambda s=None: True)
    monkeypatch.setattr(gallerydl, "fetch_replies", _fetch)
    details = _details(1)
    conn = get_connection()
    try:
        for _ in range(3):
            asyncio.run(tw_poller._capture_replies(conn, _Client(), details, 1, {}))
        assert len(fetched) == 1
        details[0]["replies"] = 2                    # a new reply: read again
        asyncio.run(tw_poller._capture_replies(conn, _Client(), details, 1, {}))
        assert len(fetched) == 2
    finally:
        conn.close()


def test_no_reads_when_throttled_or_without_gallerydl_or_cookies(monkeypatch):
    tw_poller._REPLIES_SEEN.clear()

    async def _fetch(*a, **k):
        raise AssertionError("must not read")

    monkeypatch.setattr(gallerydl, "fetch_replies", _fetch)
    conn = get_connection()
    try:
        monkeypatch.setattr(gallerydl, "is_enabled", lambda s=None: False)
        asyncio.run(tw_poller._capture_replies(conn, _Client(), _details(1), 1, {}))
        monkeypatch.setattr(gallerydl, "is_enabled", lambda s=None: True)
        throttled = _Client()
        throttled.throttled = True
        asyncio.run(tw_poller._capture_replies(conn, throttled, _details(1), 1, {}))
        official_only = _Client()
        official_only.auth_token = ""
        asyncio.run(tw_poller._capture_replies(conn, official_only, _details(1), 1, {}))
    finally:
        conn.close()


def test_own_replies_are_stored_as_ours(monkeypatch):
    tw_poller._REPLIES_SEEN.clear()

    async def _fetch(*a, **k):
        return [{"comment_id": "4000000000000000002", "author": "SecondFur", "body": "thanks!",
                 "commented_at": "2026-10-15T20:03:45Z", "permalink": ""}]

    monkeypatch.setattr(gallerydl, "is_enabled", lambda s=None: True)
    monkeypatch.setattr(gallerydl, "fetch_replies", _fetch)
    conn = get_connection()
    try:
        asyncio.run(tw_poller._capture_replies(
            conn, _Client(), [{"tweet_id": "4000000000000000001", "replies": 1, "content_type": "tweet"}], 1, {}))
        item = next(i for i in inbox_queries.get_inbox(conn, platform="tw")
                    if i["comment_id"] == "4000000000000000002")
    finally:
        conn.close()
    assert item["is_own"] and item["handled"]


def _seed_tw_comment():
    conn = get_connection()
    try:
        inbox_queries.upsert_platform_comment(conn, "tw", "5000000000000000002", "5000000000000000001",
                                              author="Inkwolf", body="hi", commented_at="2026-10-15T20:03:45Z")
    finally:
        conn.close()


def _api():
    from routes.inbox_api import inbox_router
    app = FastAPI()
    app.include_router(inbox_router)
    return TestClient(app)


def test_reply_from_the_inbox_posts_under_their_reply(monkeypatch):
    _seed_tw_comment()
    sent = {}

    class _TW:
        last_error = ""

        def __init__(self, **kw):
            sent["creds"] = kw

        async def create_tweet(self, text, reply_to=""):
            sent.update(text=text, reply_to=reply_to)
            return {"id": "5000000000000000003", "url": "https://x.com/SecondFur/status/5000000000000000003"}

        async def close(self):
            pass

    import clients.tw.client as twc
    import posting.post_publisher as pp
    monkeypatch.setattr(twc, "TWClient", _TW)
    monkeypatch.setattr(pp, "_resolve_creds", lambda *a: (1, {"tw_auth_token": "tok", "tw_ct0": "ct0",
                                                               "tw_target_user": "SecondFur"}))
    r = _api().post("/api/inbox/reply", json={"platform": "tw", "comment_id": "5000000000000000002",
                                              "text": "thank you"})
    assert r.status_code == 200, r.text
    assert sent["reply_to"] == "5000000000000000002" and sent["text"] == "thank you"
    item = next(i for i in inbox_queries.get_inbox(get_connection(), platform="tw")
                if i["comment_id"] == "5000000000000000002")
    assert item["handled"]


def test_reply_refuses_a_never_post_account(monkeypatch):
    _seed_tw_comment()
    import posting.post_publisher as pp
    from database import accounts as accounts_db
    monkeypatch.setattr(pp, "_resolve_creds", lambda *a: (7, {"tw_auth_token": "tok", "tw_ct0": "ct0"}))
    monkeypatch.setattr(accounts_db, "never_post_ids", lambda: {7})
    r = _api().post("/api/inbox/reply", json={"platform": "tw", "comment_id": "5000000000000000002",
                                              "text": "thank you"})
    assert r.status_code == 403
