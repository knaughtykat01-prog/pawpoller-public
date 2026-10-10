"""Facebook Page stats and comments (fb) — spec 029 (4.59.0).

A fake Graph Page with a photo, a video and a text post answers every call, including the batch
endpoint. Names are placeholders ("Sample Page", "Inkwolf").
"""
from __future__ import annotations

import asyncio
import json
from urllib.parse import parse_qs

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

import config
from clients.fb.client import FbClient
from database import fb_queries, inbox_queries
from database.db import get_connection

POSTS = [
    {"id": "1001_1", "created_time": "2026-10-09T05:00:00+0000", "message": "A new piece\nmore words",
     "permalink_url": "https://www.facebook.com/x/posts/1", "attachments": {"data": [{"media_type": "photo"}]}},
    {"id": "1001_2", "created_time": "2026-10-08T05:00:00+0000", "message": "A clip",
     "permalink_url": "https://www.facebook.com/x/posts/2",
     "attachments": {"data": [{"media_type": "video", "target": {"id": "vid7"}}]}},
    {"id": "1001_3", "created_time": "2026-10-07T05:00:00+0000", "message": "Just words",
     "permalink_url": "https://www.facebook.com/x/posts/3"},
]


class FakePage:
    def __init__(self, *, views=120, comments=2, followers=31, insights_error=None, bad_metric="",
                 comments_error=None, posts=None):
        self.views, self.comments, self.followers = views, comments, followers
        self.insights_error, self.bad_metric, self.comments_error = insights_error, bad_metric, comments_error
        self.posts = POSTS if posts is None else posts
        self.replies: list[tuple[str, str]] = []

    def _counts(self, pid):
        return {"id": pid, "comments": {"summary": {"total_count": self.comments}},
                "reactions": {"summary": {"total_count": 9}}, "shares": {"count": 1}}

    def _insights(self, url):
        metrics = url.split("metric=", 1)[1].split(",")
        if self.insights_error:
            return {"error": self.insights_error}
        if self.bad_metric and self.bad_metric in metrics:
            return {"error": {"code": 100, "message": f"(#100) The value must be a valid insights metric: {self.bad_metric}"}}
        data = []
        for m in metrics:
            val = {"post_media_view": self.views, "post_video_views": 40,
                   "post_reactions_by_type_total": {"like": 7, "love": 2}}[m]
            data.append({"name": m, "values": [{"value": val}]})
        return {"data": data}

    def __call__(self, req: httpx.Request) -> httpx.Response:
        path, q = req.url.path, dict(req.url.params)
        form = {k: v[0] for k, v in parse_qs(req.content.decode()).items()} if req.method == "POST" else {}
        if req.method == "POST" and "batch" in form:
            out = []
            for r in json.loads(form["batch"]):
                url = r["relative_url"]
                body = self._insights(url) if "/insights" in url else self._counts(url.split("?")[0])
                out.append({"code": 400 if "error" in body else 200, "body": json.dumps(body)})
            return httpx.Response(200, json=out)
        if path.endswith("/1001/posts"):
            return httpx.Response(200, json={"data": self.posts})
        if path.endswith("/comments") and req.method == "GET":
            if self.comments_error:
                return httpx.Response(400, json={"error": self.comments_error})
            return httpx.Response(200, json={"data": [
                {"id": "c1", "from": {"id": "77", "name": "Inkwolf"}, "message": "love it",
                 "created_time": "2026-10-09T06:00:00+0000", "permalink_url": "https://www.facebook.com/c1"},
                {"id": "c2", "from": {"id": "1001", "name": "Sample Page"}, "message": "thank you!",
                 "created_time": "2026-10-09T07:00:00+0000", "parent": {"id": "c1"}},
            ][: self.comments]})
        if path.endswith("/comments") and req.method == "POST":
            self.replies.append((path.split("/")[-2], form.get("message", "")))
            return httpx.Response(200, json={"id": "c9"})
        if path.endswith("/1001") and q.get("fields") == "followers_count":
            return httpx.Response(200, json={"followers_count": self.followers})
        return httpx.Response(404, json={"error": {"code": 803, "message": "unknown"}})


def _client(fake):
    return FbClient("PAGE-TOKEN", "1001", transport=httpx.MockTransport(fake))


def _account():
    from database import accounts as adb
    config.save_settings({"fb_page_token": "PAGE-TOKEN", "fb_page_id": "1001", "fb_page_name": "Sample Page"})
    conn = get_connection()
    try:
        return adb.get_default_account_id(conn, "fb", create=True)
    finally:
        conn.close()


def _poll(aid, fake, **kw):
    from polling.fb_poller import run_fb_poll_cycle
    return asyncio.run(run_fb_poll_cycle(aid, client=_client(fake), **kw))


def _q(sql, *args):
    conn = get_connection()
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


# ── numbers ──────────────────────────────────────────────────────────────────

def test_a_poll_stores_every_posts_numbers():
    aid = _account()
    stats = _poll(aid, FakePage())
    assert stats["submissions_found"] == 3 and stats["snapshots_inserted"] == 3
    conn = get_connection()
    try:
        photo = fb_queries.get_post(conn, "1001_1")
        assert (photo["views"], photo["reactions"], photo["comments"], photo["shares"]) == (120, 9, 2, 1)
        assert photo["reactions_by_type"] == {"like": 7, "love": 2}
        assert photo["title"] == "A new piece" and photo["content_type"] == "photo"
        video = fb_queries.get_post(conn, "1001_2")
        assert video["plays"] == 40 and video["video_id"] == "vid7" and video["content_type"] == "video"
        assert fb_queries.get_post(conn, "1001_3")["plays"] is None      # not a video: no plays figure
        s = fb_queries.get_summary(conn)
        assert s["total_views"] == 360 and s["followers"] == 31
        assert fb_queries.get_fb_last_poll(conn)["status"] == "success"
    finally:
        conn.close()


def test_a_second_poll_snapshots_only_what_moved():
    aid = _account()
    _poll(aid, FakePage())
    assert _poll(aid, FakePage())["snapshots_inserted"] == 0            # nothing moved
    assert _poll(aid, FakePage(views=150))["snapshots_inserted"] == 3
    rows = _q("SELECT views FROM fb_snapshots WHERE submission_id = '1001_1' ORDER BY id")
    assert [r[0] for r in rows] == [120, 150]


def test_an_invalid_metric_is_dropped_not_fatal():
    aid = _account()
    _poll(aid, FakePage(bad_metric="post_video_views"))
    conn = get_connection()
    try:
        video = fb_queries.get_post(conn, "1001_2")
        assert video["plays"] is None and video["views"] == 120         # retried without the bad metric
    finally:
        conn.close()


def test_missing_read_insights_keeps_counts_and_names_the_permission():
    aid = _account()
    _poll(aid, FakePage(insights_error={"code": 10, "message": "(#10) Application does not have permission for this action"}))
    conn = get_connection()
    try:
        photo = fb_queries.get_post(conn, "1001_1")
        assert photo["views"] is None and photo["comments"] == 2       # counts still arrive
        s = fb_queries.get_summary(conn)
        assert s["total_views"] is None                                 # "not available", not 0
        last = fb_queries.get_fb_last_poll(conn)
        assert last["status"] == "partial" and last["missing_permission"] == "read_insights"
    finally:
        conn.close()


def test_a_post_gone_from_facebook_is_kept_and_flagged():
    aid = _account()
    _poll(aid, FakePage())
    _poll(aid, FakePage(posts=POSTS[:2]))
    rows = dict(_q("SELECT submission_id, deleted FROM fb_submissions"))
    assert rows == {"1001_1": 0, "1001_2": 0, "1001_3": 1}


def test_a_video_publication_is_rekeyed_to_its_post():
    conn = get_connection()
    try:
        conn.execute("INSERT INTO masterpiece_members (masterpiece_name, platform, submission_id) "
                     "VALUES ('Sample Story', 'fb', 'vid7')")
        conn.commit()
    finally:
        conn.close()
    _poll(_account(), FakePage())
    assert _q("SELECT submission_id FROM masterpiece_members WHERE platform = 'fb'")[0][0] == "1001_2"
    assert _q("SELECT made_by_pawpoller FROM fb_submissions WHERE submission_id = '1001_2'")[0][0] == 1


def test_fb_is_a_polled_platform_now():
    from database import accounts, platform_metrics
    from polling.multi_account import get_poll_cycles
    assert "fb" not in accounts.POST_ONLY_PLATFORMS
    assert "fb" in get_poll_cycles()
    assert platform_metrics.get("fb").faves == "reactions"


# ── followers ────────────────────────────────────────────────────────────────

def test_page_followers_are_recorded_each_poll():
    aid = _account()
    _poll(aid, FakePage(followers=31))
    _poll(aid, FakePage(followers=36))
    rows = _q("SELECT followers FROM account_follower_snapshots WHERE account_id = ? ORDER BY rowid", aid)
    assert [r[0] for r in rows] == [31, 36]


# ── comments → Inbox ─────────────────────────────────────────────────────────

def test_comments_land_in_the_inbox_once_and_the_pages_own_is_flagged():
    aid = _account()
    stats = _poll(aid, FakePage())
    assert stats["new_comments"] == 1                     # the Page's own reply isn't "new engagement"
    _poll(aid, FakePage())
    conn = get_connection()
    try:
        items = [i for i in inbox_queries.get_inbox(conn) if i["platform"] == "fb"]
    finally:
        conn.close()
    by_id = {i["comment_id"]: i for i in items}
    assert set(by_id) == {"c1", "c2"}                      # stored once each, across three posts
    assert by_id["c1"]["can_reply"] is True and not by_id["c1"].get("is_own")
    assert by_id["c2"].get("is_own")


def test_missing_comment_permission_names_it():
    aid = _account()
    _poll(aid, FakePage(comments_error={"code": 10, "message": "(#10) This endpoint requires the "
                                        "'pages_read_user_content' permission"}))
    last = _q("SELECT status, missing_permission FROM fb_poll_log ORDER BY id DESC")[0]
    assert tuple(last) == ("partial", "pages_read_user_content")


def _inbox_api():
    from routes.inbox_api import inbox_router
    app = FastAPI()
    app.include_router(inbox_router)
    return TestClient(app)


def test_an_inbox_reply_goes_under_the_comment(monkeypatch):
    import clients.fb.client as fbmod
    aid = _account()
    fake = FakePage()
    _poll(aid, fake)
    monkeypatch.setattr(fbmod, "FbClient", lambda tok, pid: FbClient(tok, pid, transport=httpx.MockTransport(fake)))
    r = _inbox_api().post("/api/inbox/reply", json={"platform": "fb", "comment_id": "c1", "text": "thanks!"})
    assert r.status_code == 200, r.text
    assert fake.replies == [("c1", "thanks!")]
    # A reply to a reply goes under the top comment (Facebook threads two deep).
    r = _inbox_api().post("/api/inbox/reply", json={"platform": "fb", "comment_id": "c2", "text": "x"})
    assert r.status_code == 200 and fake.replies[-1][0] == "c1"


def test_an_inbox_reply_without_the_permission_says_what_to_add(monkeypatch):
    import clients.fb.client as fbmod
    aid = _account()
    _poll(aid, FakePage())

    class Refusing(FakePage):
        def __call__(self, req):
            if req.method == "POST" and req.url.path.endswith("/comments"):
                return httpx.Response(403, json={"error": {"code": 200, "message":
                                      "(#200) Requires pages_manage_engagement permission"}})
            return super().__call__(req)
    monkeypatch.setattr(fbmod, "FbClient", lambda tok, pid: FbClient(tok, pid, transport=httpx.MockTransport(Refusing())))
    r = _inbox_api().post("/api/inbox/reply", json={"platform": "fb", "comment_id": "c1", "text": "hi"})
    assert r.status_code == 400 and "pages_manage_engagement" in r.json()["detail"]


def test_an_inbox_reply_from_a_never_post_account_is_refused(monkeypatch):
    import clients.fb.client as fbmod
    aid = _account()
    _poll(aid, FakePage())
    config.save_settings({"never_post_account_ids": [aid]})
    monkeypatch.setattr(fbmod, "FbClient", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no client")))
    r = _inbox_api().post("/api/inbox/reply", json={"platform": "fb", "comment_id": "c1", "text": "hi"})
    assert r.status_code == 403


def test_a_page_on_a_second_account_counts_as_connected():
    """The brand Page is account 40, not the default: its keys are stored per account, so the bare
    keys are empty. Health must still call Facebook connected, or the dashboard shows "not set up"."""
    from database import accounts as adb
    from routes.api import _health_snapshot
    conn = get_connection()
    try:
        conn.execute("INSERT INTO accounts (account_id, platform, label, handle, is_default, enabled) "
                     "VALUES (40, 'fb', 'Brand', 'Sample Page', 0, 1)")
        conn.commit()
    finally:
        conn.close()
    config.save_settings({config.account_setting_key(40, "fb_page_token", False): "PAGE-TOKEN",
                          config.account_setting_key(40, "fb_page_id", False): "1001"})
    assert not adb.DEFAULT_CRED_CHECKS["fb"](config.get_settings())     # bare keys empty
    assert _health_snapshot()["fb"]["configured"] is True
