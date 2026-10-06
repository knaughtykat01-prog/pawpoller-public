"""Spec 021 — paired comments: a reply from the same account under your own post.

The rules pinned here: the comment goes under the post (or a thread's last part) from
the account the post used; a comment that fails never changes its post's result;
Retry comment sends only the comment; a failure that can pass on its own is retried
after 5 min / 30 min / 2 h and then stops; one that needs a person never is; a piece's
{link} can point at a site posted earlier in the same run, and an empty one skips the
comment rather than posting a raw placeholder.

Placeholder names only (Sample Story, Inkwolf). No network: clients and posters are fakes.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

import config
from database import comment_queries as cq
from database import posting_queries
from database import posts_queries as q
from database.db import get_connection
from posting import paired_comment as pc
from posting import post_publisher


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


@pytest.fixture
def conn():
    c = get_connection()
    yield c
    c.close()


@pytest.fixture
def replies(monkeypatch):
    """Fake send_reply: records each call; `fail` maps platform → error to return."""
    calls, fail = [], {}

    async def fake(platform, account_id, parent, text, **kw):
        calls.append({"platform": platform, "account_id": account_id, "parent": parent, "text": text, **kw})
        if platform in fail:
            return {"success": False, "external_id": "", "external_url": "", "error": fail[platform],
                    "account_id": account_id or 0}
        return {"success": True, "external_id": f"{platform}-reply", "external_url": f"https://x/{platform}/r",
                "error": "", "account_id": account_id or 0}
    monkeypatch.setattr(pc, "send_reply", fake)
    return calls, fail


@pytest.fixture
def fake_posts(monkeypatch):
    """Fake _publish_one: every site succeeds, with Bluesky refs like the real one."""
    async def one(post, platform, account_id, settings):
        r = {"platform": platform, "account_id": account_id or 7, "success": True,
             "external_id": f"{platform}-{post['post_id']}", "external_url": f"https://x/{platform}/p", "error": ""}
        if platform == "bsky":
            r["_refs"] = {"uri": r["external_id"], "cid": "cid-root"}
        return r
    monkeypatch.setattr(post_publisher, "_publish_one", one)


def _post(conn, body="hello"):
    return q.create_post(conn, body=body, now="t")


# ── Storage ───────────────────────────────────────────────────────────────────

class TestStorage:
    def test_pending_rewrite_keeps_a_posted_row(self, conn):
        rid = cq.put_pending(conn, "post", 1, 0, "bsky", "first", now="t")
        cq.put_pending(conn, "post", 1, 0, "bsky", "second", now="t")
        assert cq.get(conn, rid)["text"] == "second"
        cq.mark(conn, rid, "posted", now="t")
        assert cq.put_pending(conn, "post", 1, 0, "bsky", "third", now="t") is None
        assert cq.get(conn, rid)["text"] == "second"

    def test_claim_due_claims_once(self, conn):
        rid = cq.put_pending(conn, "post", 1, 0, "bsky", "x", now="t")
        cq.mark(conn, rid, "failed", now="t", next_try_at="2000-01-01 00:00:00")
        assert [r["id"] for r in cq.claim_due(conn, _now())] == [rid]
        assert cq.claim_due(conn, _now()) == []

    def test_deleting_a_post_deletes_its_comments(self, conn):
        pid = _post(conn)
        cq.put_pending(conn, "post", pid, 0, "bsky", "x", now="t")
        q.delete_post(conn, pid)
        assert cq.rows_for_owner(conn, "post", pid) == []

    def test_classified(self):
        import datamap
        assert datamap.table_class("paired_comments")
        assert datamap.setting_class("comment_templates") and datamap.setting_class("comment_defaults")


# ── Posts: the pass ───────────────────────────────────────────────────────────

class TestPostsPass:
    def test_comment_goes_under_each_post_as_the_same_account(self, conn, replies, fake_posts):
        calls, _ = replies
        pid = _post(conn)
        pc.store_post_comments(pid, {"bsky": "link: https://e.x/1", "mast": "more here"})
        res = asyncio.run(post_publisher.publish_post(pid, ["bsky", "mast"], {"bsky": 3}))
        by = {c["platform"]: c for c in calls}
        assert by["bsky"]["parent"]["uri"] == f"bsky-{pid}" and by["bsky"]["parent"]["cid"] == "cid-root"
        assert by["bsky"]["account_id"] == 3
        assert by["mast"]["parent"]["id"] == f"mast-{pid}"
        assert all(r["comment"]["status"] == "posted" for r in res)

    def test_thread_comment_goes_under_the_last_part(self, conn, replies, fake_posts, monkeypatch):
        calls, _ = replies
        pid = _post(conn)
        q.create_post(conn, body="part 2", now="t", parent_post_id=pid, thread_ordinal=1)

        async def parts(parts, platform, account_id, settings, parent_res):
            return [{"platform": platform, "account_id": 7, "success": True, "part": p["post_id"],
                     "external_id": f"part-{p['post_id']}", "external_url": "", "error": ""} for p in parts]
        monkeypatch.setattr(post_publisher, "_publish_thread_parts", parts)
        pc.store_post_comments(pid, {"bsky": "the end"})
        asyncio.run(post_publisher.publish_post(pid, ["bsky"]))
        p = calls[0]["parent"]
        assert p["uri"].startswith("part-") and p["root_uri"] == f"bsky-{pid}" and p["root_cid"] == "cid-root"

    def test_a_failed_comment_never_fails_the_post(self, conn, replies, fake_posts):
        calls, fail = replies
        fail["bsky"] = "rate limited, slow down"
        pid = _post(conn)
        pc.store_post_comments(pid, {"bsky": "x"})
        res = asyncio.run(post_publisher.publish_post(pid, ["bsky"]))
        assert res[0]["success"] is True and not res[0]["error"]
        assert res[0]["comment"]["status"] == "failed"
        pub = q.get_post_publications(conn, pid)[0]
        assert pub["status"] == "posted"

    def test_main_post_failed_leaves_the_comment_waiting(self, conn, replies, monkeypatch):
        calls, _ = replies

        async def one(post, platform, account_id, settings):
            return {"platform": platform, "account_id": 0, "success": False, "external_id": "",
                    "external_url": "", "error": "down"}
        monkeypatch.setattr(post_publisher, "_publish_one", one)
        pid = _post(conn)
        pc.store_post_comments(pid, {"bsky": "x"})
        asyncio.run(post_publisher.publish_post(pid, ["bsky"]))
        assert calls == []
        assert cq.find(conn, "post", pid, 0, "bsky")["status"] == "pending"

    def test_tumblr_gets_no_comment_row(self, conn):
        pid = _post(conn)
        pc.store_post_comments(pid, {"tum": "x", "bsky": "y"})
        assert {r["platform"] for r in cq.rows_for_owner(conn, "post", pid)} == {"bsky"}

    def test_preview_explains_comment_limits_and_tumblr(self):
        out = post_publisher.preview("hi", ["tw", "tum"], comments={"tw": "x" * 300, "tum": "y"})
        assert any(w["level"] == "block" for w in out["tw"]["comment"]["warnings"])
        assert "no replies" in out["tum"]["comment"]["warnings"][0]["text"]

    def test_unknown_placeholder_refused(self, conn):
        pid = _post(conn)
        with pytest.raises(ValueError):
            pc.store_post_comments(pid, {"bsky": "see {lnk}"})


# ── Retry ─────────────────────────────────────────────────────────────────────

class TestRetry:
    def _failed(self, conn, error, attempts=0):
        pid = _post(conn)
        q.upsert_post_publication(conn, post_id=pid, platform="bsky", account_id=7, status="posted",
                                  external_id=f"bsky-{pid}", now="t")
        rid = cq.put_pending(conn, "post", pid, 0, "bsky", "x", now="t")
        row = cq.get(conn, rid)
        row["attempts"] = attempts
        return pc._record(row, {"success": False, "error": error, "account_id": 7}, "p", automatic=attempts > 0)

    def test_ladder_5_30_120_then_stop(self, conn):
        def gap(row):
            t = datetime.strptime(row["next_try_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            return round((t - datetime.now(timezone.utc)).total_seconds() / 60)
        assert gap(self._failed(conn, "timeout")) == 5
        r = self._failed(conn, "timeout", attempts=1)
        assert r["attempts"] == 2 and gap(r) == 120
        assert self._failed(conn, "timeout", attempts=3)["next_try_at"] == ""

    def test_permanent_failure_is_never_rescheduled(self, conn):
        assert self._failed(conn, "Threads needs the manage-replies permission")["next_try_at"] == ""
        assert self._failed(conn, "invalid_grant")["next_try_at"] == ""

    def test_manual_retry_sends_only_the_comment(self, conn, replies, monkeypatch):
        calls, _ = replies
        published = []
        monkeypatch.setattr(post_publisher, "_publish_one", lambda *a, **k: published.append(a))
        row = self._failed(conn, "timeout")
        out = asyncio.run(pc.resend(row["id"]))
        assert out["status"] == "posted" and published == []
        assert calls[0]["parent"]["id"].startswith("bsky-") and calls[0]["account_id"] == 7

    def test_retry_refused_when_the_post_is_not_live(self, conn):
        pid = _post(conn)
        rid = cq.put_pending(conn, "post", pid, 0, "bsky", "x", now="t")
        with pytest.raises(RuntimeError):
            asyncio.run(pc.resend(rid))

    def test_run_due_sends_due_rows(self, conn, replies):
        calls, _ = replies
        row = self._failed(conn, "timeout")
        cq.mark(conn, row["id"], "failed", now="t", next_try_at="2000-01-01 00:00:00")
        assert asyncio.run(pc.run_due({})) == 1
        assert cq.get(conn, row["id"])["status"] == "posted" and cq.get(conn, row["id"])["attempts"] == 1


# ── Templates + placeholders ──────────────────────────────────────────────────

class TestTemplates:
    def test_validation(self):
        good, d = pc.validate_templates([{"name": "Links", "text": "Full: {link}"}], {"tw": "Links"})
        assert d == {"tw": "Links"}
        for bad in ([{"name": "A", "text": "{nope}"}], [{"name": "A", "text": "x"}, {"name": "a", "text": "y"}],
                    [{"name": "", "text": "x"}]):
            with pytest.raises(ValueError):
                pc.validate_templates(bad, {})
        with pytest.raises(ValueError):
            pc.validate_templates([{"name": "A", "text": "x"}], {"tum": "A"})
        with pytest.raises(ValueError):
            pc.validate_templates([{"name": "A", "text": "x"}], {"tw": "B"})

    def test_fill(self):
        ctx = {"extra": {"links_by_platform": [("fa", "https://fa/1"), ("ws", "https://ws/2")]}, "title": "Sample Story"}
        text, empty = pc.fill("{title}: {link} | {site:ws}\n{links}", ctx, "tw", {})
        assert text == "Sample Story: https://fa/1 | https://ws/2\nhttps://fa/1\nhttps://ws/2" and empty == []
        text, empty = pc.fill("{link}", {"extra": {}}, "tw", {})
        assert empty == ["link"]

    def test_link_mode_none_gives_no_link(self):
        ctx = {"extra": {"links_by_platform": [("fa", "https://fa/1")], "link_mode": "none"}}
        assert pc.fill("{link}", ctx, "tw", {})[1] == ["link"]

    def test_templates_route_drops_defaults_of_deleted_templates(self):
        from routes import comments_api
        comments_api.put_templates({"templates": [{"name": "Links", "text": "{link}"}], "defaults": {"tw": "Links"}})
        out = comments_api.put_templates({"templates": [], "defaults": {"tw": "Links"}})
        assert out["defaults"] == {}
        assert config.get_settings()["comment_defaults"] == {}


# ── Pieces ────────────────────────────────────────────────────────────────────

class TestPieces:
    def test_defaults_apply_only_when_comments_absent(self, conn):
        config.save_settings({"comment_templates": [{"name": "L", "text": "Full: {link}"}],
                              "comment_defaults": {"tw": "L"}})
        pc.store_piece_comments("artwork", "Sample_Piece", ["fa", "tw", "bsky"], None)
        rows = {r["platform"]: r for r in cq.rows_for_owner(conn, "artwork", "Sample_Piece")}
        assert set(rows) == {"tw"} and rows["tw"]["template"] == "L"
        pc.store_piece_comments("artwork", "Sample_Piece", ["tw"], {})       # cleared in the dialog
        assert cq.rows_for_owner(conn, "artwork", "Sample_Piece") == []

    def test_link_from_this_run_and_skip_when_missing(self, conn, replies, monkeypatch):
        calls, _ = replies
        monkeypatch.setattr(pc, "piece_context", lambda *a, **k: {"extra": {}, "title": "T"})
        pc.store_piece_comments("artwork", "Sample_Piece", ["tw", "bsky"],
                                {"tw": "Full: {site:fa}", "bsky": "Full: {site:ws}"})
        results = [
            {"platform": "fa", "chapter_index": 0, "success": True, "external_id": "1", "external_url": "https://fa/1"},
            {"platform": "tw", "chapter_index": 0, "success": True, "external_id": "t1", "account_id": 4,
             "external_url": "https://x/t1"},
            {"platform": "tw", "chapter_index": 0, "success": True, "external_id": "t2", "account_id": 4},
            {"platform": "bsky", "chapter_index": 0, "success": True, "external_id": "b1", "account_id": 5},
        ]
        asyncio.run(pc.comment_pass_pieces("artwork", "Sample_Piece", results))
        assert [c["platform"] for c in calls] == ["tw"]                      # once per site, first render
        assert calls[0]["text"] == "Full: https://fa/1" and calls[0]["parent"]["id"] == "t1"
        assert results[3]["comment"]["status"] == "skipped"

    def test_publish_route_stores_comments(self, conn, monkeypatch):
        from routes import artwork_api
        from posting import manager

        async def fake_post_artwork(name, platforms, **kw):
            return []
        monkeypatch.setattr(manager, "post_artwork", fake_post_artwork)
        asyncio.run(artwork_api.publish_artwork({"artwork_name": "Sample_Piece", "platforms": ["tw"],
                                                 "confirm_live": True, "comments": {"tw": "hi"}}))
        assert cq.find(conn, "artwork", "Sample_Piece", 0, "tw")["text"] == "hi"


# ── Clients build the right reply request ─────────────────────────────────────

class TestClients:
    def test_x_reply_variables(self, monkeypatch):
        from clients.tw.client import TWClient
        c = TWClient(auth_token="a", ct0="b", target_user="someone")
        seen = {}

        async def gql(qid, name, variables, features):
            seen.update(variables)
            return {"data": {"create_tweet": {"tweet_results": {"result": {"rest_id": "9"}}}}}
        monkeypatch.setattr(c, "_post_graphql", gql)
        asyncio.run(c.create_tweet("hi", reply_to="5"))
        assert seen["reply"] == {"in_reply_to_tweet_id": "5", "exclude_reply_user_ids": []}

    def test_threads_reply_to_id(self, monkeypatch):
        from clients.thr.client import ThrClient
        c = ThrClient(access_token="t", user_id="u")
        forms = []

        async def ok():
            return True

        async def post_form(url, form):
            forms.append(form)
            return {"id": "1"}

        async def get_json(url, params=None):
            return {"permalink": "p"}
        monkeypatch.setattr(c, "ensure_logged_in", ok)
        monkeypatch.setattr(c, "_post_form", post_form)
        monkeypatch.setattr(c, "_get_json", get_json)
        asyncio.run(c.create_thread("hi", reply_to="77"))
        assert forms[0]["reply_to_id"] == "77"

    def test_telegram_reply_parameters(self, monkeypatch):
        from clients.tg import client as tgmod
        sent = {}

        async def fake_send(self, client, text, common=None, preview=True):
            sent.update(common or {})
            return {"id": "2", "url": ""}
        monkeypatch.setattr(tgmod.TgClient, "_send_message", fake_send)
        asyncio.run(tgmod.TgClient(bot_token="b", channel="@c").create_post("hi", reply_to="41"))
        assert json.loads(sent["reply_parameters"]) == {"message_id": 41}

    def test_instagram_comment(self, monkeypatch):
        from clients.ig.client import IgClient
        c = IgClient(access_token="t", user_id="u")
        seen = {}

        async def ok():
            return True

        async def post_json(url, data):
            seen["url"], seen["data"] = url, data
            return {"id": "c1"}
        monkeypatch.setattr(c, "ensure_logged_in", ok)
        monkeypatch.setattr(c, "_post_json", post_json)
        assert asyncio.run(c.create_comment("m1", "hi")) == {"id": "c1"}
        assert seen["url"].endswith("/m1/comments") and seen["data"] == {"message": "hi"}

    def test_never_post_account_refused(self, monkeypatch):
        from database import accounts as accounts_db
        monkeypatch.setattr(accounts_db, "never_post_ids", lambda s=None: {9})
        monkeypatch.setattr(post_publisher, "_resolve_creds", lambda p, a, s: (9, {}))
        out = asyncio.run(pc.send_reply("tw", 9, {"id": "1"}, "hi"))
        assert not out["success"] and out["error"] == accounts_db.NEVER_POST_ERROR


class TestActivity:
    def test_a_failed_comment_line_never_fails_the_job(self):
        from posting import activity
        activity._reset_for_tests()
        jid = activity.start("post", "Post", ["bsky"])
        with activity.bound(jid):
            activity.line_done("bsky", True)
            activity.add_line("bsky:comment", "Bluesky — comment", aside=True)
            activity.line_done("bsky:comment", False, error="rate limited")
        activity.finish(jid)
        job = next(j for j in activity.snapshot() if j["id"] == jid)
        assert job["state"] == "done"
        assert any(l.get("aside") for l in job["lines"])