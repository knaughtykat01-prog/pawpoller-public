"""Spec 018 — the Posts feed, composer and tag contacts, redone.

The feed now answers "how did that post do" for every post at once, which only works
if the numbers come from ONE pass over the list (the per-platform batched helper the
post page already uses) and if "not measured" never reads as 0. The composer asks the
server how each site will treat the post (the publisher's own `_render_body`, not a
second copy in the browser). Contacts gain an alias, a "tagged in" count, and
suggestions taken from the operator's own posts.

All names here are placeholders (Inkwolf / Penwright / SecondFur).
"""
from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

import config
from database import accounts as accounts_db
from database import posting_queries
from database import posts_queries as q
from database import personas as personas_db
from posting import post_publisher
from routes import posts_api


def _post(conn, body, *, created="2026-09-01 10:00:00", parent=0, ordinal=0):
    return q.create_post(conn, body=body, now=created, parent_post_id=parent,
                         thread_ordinal=ordinal)


def _pub(conn, pid, platform, *, status="posted", ext="", acct=0):
    q.upsert_post_publication(conn, post_id=pid, platform=platform, account_id=acct,
                              status=status, external_id=ext, now="t")


def _bsky_row(conn, sid, likes=0, reposts=0, replies=0):
    conn.execute("INSERT INTO bsky_submissions (submission_id, likes, reposts, replies) "
                 "VALUES (?, ?, ?, ?)", (sid, likes, reposts, replies))
    conn.commit()


def _schedule(conn, pid, platform, when="2099-01-01 20:00:00", persona_id=None, acct=None):
    return posting_queries.add_to_queue(conn, str(pid), 0, platform, content_type="post",
                                        scheduled_at=when, title_override="x",
                                        persona_id=persona_id, account_id=acct)


def _list(**kw):
    kw.setdefault("limit", 100)
    for k in ("status", "persona_id", "q"):
        kw.setdefault(k, None)
    return posts_api.list_posts(**kw)


# ── Phase 1: the list carries its numbers ─────────────────────────────

class TestFeedNumbers:
    def test_every_post_carries_per_site_stats_and_totals(self, db_conn):
        pid = _post(db_conn, "sketch dump")
        _pub(db_conn, pid, "bsky", ext="at://a/1")
        _bsky_row(db_conn, "at://a/1", likes=41, reposts=9, replies=3)
        post = next(p for p in _list()["posts"] if p["post_id"] == pid)
        pub = post["publications"][0]
        assert pub["stats"]["favorites"] == 41
        assert pub["stats"]["reposts"] == 9
        assert pub["stats"]["comments"] == 3
        assert post["totals"]["favorites"] == 41 and post["totals"]["reposts"] == 9

    def test_unpolled_is_none_never_zero(self, db_conn):
        """A post that went out but was never polled has no numbers — not zeros."""
        pid = _post(db_conn, "not polled yet")
        _pub(db_conn, pid, "bsky", ext="at://a/never")
        post = next(p for p in _list()["posts"] if p["post_id"] == pid)
        stats = post["publications"][0]["stats"]
        assert stats["favorites"] is None and stats["reposts"] is None
        assert post["totals"]["tracked"]["favorites"] is False

    def test_views_untracked_is_flagged(self, db_conn):
        pid = _post(db_conn, "bluesky has no views")
        _pub(db_conn, pid, "bsky", ext="at://a/2")
        _bsky_row(db_conn, "at://a/2", likes=5)
        post = next(p for p in _list()["posts"] if p["post_id"] == pid)
        assert post["totals"]["tracked"]["views"] is False
        assert post["totals"]["tracked"]["favorites"] is True

    def test_one_batched_stats_pass_however_many_posts(self, db_conn, monkeypatch):
        from database import collections_queries as cq
        for i in range(30):
            pid = _post(db_conn, f"post {i}")
            _pub(db_conn, pid, "bsky", ext=f"at://a/{i}")
            _pub(db_conn, pid, "mast", ext=f"m{i}")
        calls = []
        real = cq._submission_rows_bulk
        monkeypatch.setattr(cq, "_submission_rows_bulk",
                            lambda conn, pairs: calls.append(len(pairs)) or real(conn, pairs))
        out = _list()
        assert len(out["posts"]) == 30
        assert len(calls) == 1, "the whole page resolves in one batched pass"

    def test_thread_parts_stay_out_of_the_feed(self, db_conn):
        pid = _post(db_conn, "part one")
        _post(db_conn, "part two", parent=pid, ordinal=1)
        ids = [p["post_id"] for p in _list()["posts"]]
        assert ids == [pid]
        assert _list()["posts"][0]["thread_count"] == 1


# ── Phase 1: filters + counts ─────────────────────────────────────────

class TestFilters:
    def test_scheduled_filter_and_queue_rows(self, db_conn):
        sched = _post(db_conn, "coming later")
        _schedule(db_conn, sched, "bsky", when="2099-01-01 20:00:00")
        _schedule(db_conn, sched, "mast", when="2099-01-01 20:00:00")
        _post(db_conn, "already out")
        out = _list(status="scheduled")
        assert [p["post_id"] for p in out["posts"]] == [sched]
        rows = out["posts"][0]["scheduled"]
        assert {r["platform"] for r in rows} == {"bsky", "mast"}
        assert all(r["queue_id"] for r in rows)
        assert out["counts"]["scheduled"] == 1

    def test_a_cancelled_schedule_is_not_scheduled(self, db_conn):
        pid = _post(db_conn, "unscheduled")
        qid = _schedule(db_conn, pid, "bsky")
        posting_queries.cancel_queue_item(db_conn, qid)
        assert _list(status="scheduled")["posts"] == []

    def test_failed_filter(self, db_conn):
        bad = _post(db_conn, "mastodon refused")
        _pub(db_conn, bad, "bsky", ext="at://a/9")
        _pub(db_conn, bad, "mast", status="failed")
        _post(db_conn, "fine")
        out = _list(status="failed")
        assert [p["post_id"] for p in out["posts"]] == [bad]
        assert out["posts"][0]["failed_sites"] == ["mast"]
        assert out["counts"]["failed"] == 1

    def test_failed_then_succeeded_on_that_site_is_not_failed(self, db_conn):
        """A failure on one account and a success on another, same site: it posted."""
        a1 = accounts_db.create_account(db_conn, "mast", "First")
        a2 = accounts_db.create_account(db_conn, "mast", "Second")
        pid = _post(db_conn, "retried elsewhere")
        _pub(db_conn, pid, "mast", status="failed", acct=a1)
        _pub(db_conn, pid, "mast", ext="m1", acct=a2)
        assert _list(status="failed")["posts"] == []
        assert _list()["posts"][0]["failed_sites"] == []

    def test_persona_filter_via_publication_accounts(self, db_conn):
        p1 = personas_db.create_persona(db_conn, "SecondFur")
        acct = accounts_db.create_account(db_conn, "bsky", "SecondFur on Bluesky")
        personas_db.assign_account_persona(db_conn, acct, p1)
        mine = _post(db_conn, "as SecondFur")
        _pub(db_conn, mine, "bsky", ext="at://a/p", acct=acct)
        _post(db_conn, "someone else")
        out = _list(persona_id=p1)
        assert [p["post_id"] for p in out["posts"]] == [mine]
        assert out["posts"][0]["persona"]["name"] == "SecondFur"

    def test_persona_filter_includes_scheduled_for_that_persona(self, db_conn):
        p1 = personas_db.create_persona(db_conn, "SecondFur")
        pid = _post(db_conn, "scheduled as SecondFur")
        _schedule(db_conn, pid, "bsky", persona_id=p1)
        assert [p["post_id"] for p in _list(persona_id=p1)["posts"]] == [pid]

    def test_search_is_literal(self, db_conn):
        hit = _post(db_conn, "100% done with the lines")
        _post(db_conn, "1000 done")
        assert [p["post_id"] for p in _list(q="100%")["posts"]] == [hit]
        assert _list(q="nothing like this")["posts"] == []


# ── Phase 1: header + side column ─────────────────────────────────────

class TestSummary:
    def test_this_month_vs_last_in_the_operators_zone(self, db_conn, monkeypatch):
        monkeypatch.setattr(config, "display_zone", lambda: timezone.utc)
        now = datetime.now(timezone.utc)
        this_month = now.replace(day=1, hour=1).strftime("%Y-%m-%d %H:%M:%S")
        last_month = (now.replace(day=1) - timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")
        a = _post(db_conn, "this month, best", created=this_month)
        _pub(db_conn, a, "bsky", ext="at://s/1")
        _bsky_row(db_conn, "at://s/1", likes=30, reposts=4)
        b = _post(db_conn, "this month, other", created=this_month)
        _pub(db_conn, b, "bsky", ext="at://s/2")
        _bsky_row(db_conn, "at://s/2", likes=10)
        c = _post(db_conn, "last month", created=last_month)
        _pub(db_conn, c, "bsky", ext="at://s/3")
        _bsky_row(db_conn, "at://s/3", likes=7)
        _post(db_conn, "draft, never posted", created=this_month)
        s = posts_api.posts_summary()
        assert s["posts_this_month"] == 2 and s["posts_last_month"] == 1
        assert s["likes_this_month"] == 40 and s["likes_last_month"] == 7
        assert s["best_site"] == "bsky"
        assert s["best_post"]["post_id"] == a and s["best_post"]["favorites"] == 30
        assert s["by_site_30d"][0]["platform"] == "bsky"

    def test_coming_up_lists_the_next_scheduled(self, db_conn):
        pid = _post(db_conn, "next up")
        _schedule(db_conn, pid, "bsky", when="2099-01-02 20:00:00")
        _schedule(db_conn, pid, "mast", when="2099-01-02 20:00:00")
        up = posts_api.posts_summary()["coming_up"]
        assert len(up) == 1 and up[0]["post_id"] == pid
        assert set(up[0]["platforms"]) == {"bsky", "mast"}


# ── Phase 3: the composer asks the server ─────────────────────────────

def _contact(conn, name, **handles):
    return q.add_contact(conn, name=name, **handles)


class TestPreview:
    def test_handles_are_swapped_by_the_publishers_renderer(self, db_conn):
        cid = _contact(db_conn, "Inkwolf", handle_bsky="inkwolf.example", handle_tw="InkwolfArt")
        out = posts_api.preview_post({
            "body": "Lines by @inkwolf", "platforms": ["bsky", "tw", "mast"],
            "mentions": [{"token": "inkwolf", "contact_id": cid}]})["sites"]
        assert out["bsky"]["text"] == "Lines by @inkwolf.example"
        assert out["tw"]["text"] == "Lines by @InkwolfArt"
        assert out["mast"]["text"] == "Lines by @inkwolf"
        assert any("plain text" in w["text"] for w in out["mast"]["warnings"])
        assert not any("plain text" in w["text"] for w in out["bsky"]["warnings"])

    def test_each_site_has_its_own_limit(self, db_conn):
        body = "x" * 290
        out = posts_api.preview_post({"body": body, "platforms": ["bsky", "tw", "mast"]})["sites"]
        assert out["bsky"]["limit"] == 300 and out["tw"]["limit"] == 280 and out["mast"]["limit"] == 500
        assert out["tw"]["over"] is True and out["bsky"]["over"] is False
        assert any(w["level"] == "block" for w in out["tw"]["warnings"])

    def test_media_rules_are_named(self, db_conn):
        out = posts_api.preview_post({"body": "hi", "platforms": ["thr", "ig"],
                                      "image_count": 2})["sites"]
        assert any("2 images will be left off" in w["text"] and w["level"] == "warn"
                   for w in out["thr"]["warnings"])
        out = posts_api.preview_post({"body": "hi", "platforms": ["ig"]})["sites"]
        assert any("needs a photo" in w["text"] for w in out["ig"]["warnings"])

    def test_thread_parts_on_a_site_without_threads(self, db_conn):
        out = posts_api.preview_post({"body": "one", "parts": ["two", "three"],
                                      "platforms": ["bsky", "tw"]})["sites"]
        assert not any("part 1 only" in w["text"] for w in out["bsky"]["warnings"])
        assert any("part 1 only" in w["text"] for w in out["tw"]["warnings"])

    def test_a_long_thread_part_is_flagged(self, db_conn):
        out = posts_api.preview_post({"body": "ok", "parts": ["y" * 301],
                                      "platforms": ["bsky"]})["sites"]
        assert any("Part 2" in w["text"] for w in out["bsky"]["warnings"])

    def test_not_connected_is_said(self, db_conn):
        out = posts_api.preview_post({"body": "hi", "platforms": ["tg"]})["sites"]
        assert out["tg"]["connected"] is False
        assert any("isn't connected" in w["text"] for w in out["tg"]["warnings"])

    def test_rules_come_from_one_table(self):
        r = posts_api.posts_rules()
        assert r["limits"]["bsky"] == 300 and r["limits"]["tw"] == 280
        assert r["text_only"] == list(post_publisher._TEXT_ONLY)
        assert r["image_required"] == list(post_publisher._IMAGE_REQUIRED)
        assert set(r["thread_platforms"]) == {"bsky", "mast"}

    def test_length_counts_an_emoji_as_one(self):
        assert post_publisher.text_length("hi 🧡") == 4
        assert post_publisher.text_length("👍🏽") == 1          # skin tone modifier
        assert post_publisher.text_length("👨‍👩‍👧") == 1      # ZWJ family
        assert post_publisher.text_length("é") == 1            # combining accent


def test_an_unknown_platform_writes_no_publication_row(db_conn):
    pid = q.create_post(db_conn, body="hi", now="t0")
    res = asyncio.run(post_publisher.publish_post(pid, ['"><img src=x>']))
    assert res[0]["success"] is False and res[0].get("refused")
    assert q.get_post_publications(db_conn, pid) == []


def test_text_only_sites_post_the_text_and_leave_images_off(db_conn):
    """Spec 018 US4: "Threads: text only, your 2 images will be left off". The post goes
    out as text rather than being refused (it reaches the credential check, not a gate)."""
    pid = q.create_post(db_conn, body="hi", image_path="/tmp/x.png", now="t0")
    post = q.get_post(db_conn, pid)
    for plat in ("thr", "tum"):
        res = asyncio.run(post_publisher._publish_one(post, plat, None, {}))
        assert res["success"] is False
        assert "text-only" not in res["error"]


# ── Phase 4: contacts ─────────────────────────────────────────────────

class TestContacts:
    def test_used_count_from_mentions(self, db_conn):
        cid = _contact(db_conn, "Inkwolf")
        for body in ("one @inkwolf", "two @inkwolf"):
            pid = _post(db_conn, body)
            q.set_post_mentions(db_conn, pid, [{"token": "inkwolf", "contact_id": cid}])
        c = next(x for x in posts_api.list_contacts()["contacts"] if x["id"] == cid)
        assert c["used_count"] == 2

    def test_suggestions_come_from_own_posts_and_skip_saved(self, db_conn):
        _contact(db_conn, "Inkwolf")
        _post(db_conn, "lines by @inkwolf and @penwright")
        _post(db_conn, "again @Penwright! mail me at owner@example.com")
        pid = _post(db_conn, "thread start")
        _post(db_conn, "colours @thirdfur", parent=pid, ordinal=1)
        sug = {s["name"].lower(): s["count"] for s in posts_api.suggest_contacts()["suggestions"]}
        assert "inkwolf" not in sug, "already saved"
        assert sug["penwright"] == 2
        assert sug["thirdfur"] == 1, "thread parts count too"
        assert "example" not in sug, "an email is not a mention"

    def test_suggestions_skip_saved_aliases(self, db_conn):
        posts_api.create_contact({"name": "Penwright", "alias": "penwr"})
        _post(db_conn, "colours by @penwr")
        assert posts_api.suggest_contacts()["suggestions"] == []

    def test_alias_collision_is_refused(self, db_conn):
        posts_api.create_contact({"name": "Inkwolf", "alias": "ink"})
        with pytest.raises(HTTPException) as e:
            posts_api.create_contact({"name": "Inky", "alias": "INK"})
        assert e.value.status_code == 409 and "ink" in e.value.detail.lower()
        with pytest.raises(HTTPException):
            posts_api.create_contact({"name": "ink"})      # a name used as someone's alias

    def test_editing_a_contact_keeps_its_own_alias(self, db_conn):
        c = posts_api.create_contact({"name": "Inkwolf", "alias": "ink"})["contact"]
        out = posts_api.update_contact(c["id"], {"name": "Inkwolf", "alias": "ink",
                                                 "handle_bsky": "inkwolf.example"})
        assert out["contact"]["handle_bsky"] == "inkwolf.example"

    def test_alias_column_migrates_onto_an_old_table(self, tmp_path, monkeypatch):
        """Upgrade path: a post_contacts table from before 018 boots clean and gains alias."""
        db = tmp_path / "old.db"
        monkeypatch.setattr(config, "DB_PATH", db)
        c = sqlite3.connect(db)
        c.execute("CREATE TABLE post_contacts (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                  "name TEXT NOT NULL DEFAULT '', handle_bsky TEXT NOT NULL DEFAULT '', "
                  "handle_tw TEXT NOT NULL DEFAULT '', handle_mast TEXT NOT NULL DEFAULT '', "
                  "handle_thr TEXT NOT NULL DEFAULT '', handle_tum TEXT NOT NULL DEFAULT '', "
                  "created_at TEXT NOT NULL DEFAULT (datetime('now')))")
        c.execute("INSERT INTO post_contacts (name) VALUES ('Inkwolf')")
        c.commit()
        c.close()
        from database.db import init_db, get_connection
        init_db()
        conn = get_connection()
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(post_contacts)")}
            assert {"alias", "handle_checks"} <= cols
            assert conn.execute("SELECT alias FROM post_contacts").fetchone()[0] == ""
        finally:
            conn.close()


class TestHandleCheck:
    def test_private_hosts_are_refused(self):
        for host in ("localhost", "127.0.0.1", "10.0.0.5", "169.254.169.254", "[::1]",
                     "bad host", ""):
            assert posts_api._public_host(host) is None, host

    def test_a_name_that_resolves_privately_is_refused(self, monkeypatch):
        """The getaddrinfo / is_global branch: a real-looking name pointing inside."""
        for addr in ("10.1.2.3", "127.0.0.1", "169.254.169.254", "::1", "100.100.1.1"):
            monkeypatch.setattr(posts_api.socket, "getaddrinfo",
                                lambda *a, _x=addr, **k: [(0, 0, 0, "", (_x, 443))])
            assert posts_api._public_host("instance.example") is None, addr
        monkeypatch.setattr(posts_api.socket, "getaddrinfo",
                            lambda *a, **k: [(0, 0, 0, "", ("93.184.216.34", 443))])
        assert posts_api._public_host("instance.example") == "instance.example"

    def test_check_caches_the_result(self, db_conn, monkeypatch):
        cid = _contact(db_conn, "Inkwolf", handle_bsky="inkwolf.example",
                       handle_mast="inkwolf@example.social")

        async def fake_bsky(handle):
            return handle == "inkwolf.example"

        async def fake_mast(handle):
            return False
        monkeypatch.setattr(posts_api, "_check_bsky", fake_bsky)
        monkeypatch.setattr(posts_api, "_check_mast", fake_mast)
        out = asyncio.run(posts_api.check_contact_handles(cid))
        assert out["checks"]["bsky"]["found"] is True
        assert out["checks"]["mast"]["found"] is False
        stored = posts_api.list_contacts()["contacts"][0]["checks"]
        assert stored["bsky"]["handle"] == "inkwolf.example" and stored["bsky"]["found"] is True
