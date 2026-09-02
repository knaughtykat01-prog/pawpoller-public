"""Unit tests for the Posts (microblog) module — queries + publisher logic.

DB tests use the shared `db_conn` fixture (real init_db → posts schema applied).
Publisher tests drive the async helpers via asyncio.run (no pytest-asyncio
dependency) and never hit the network — they exercise the unsupported-platform
short-circuit and the not-connected credential path only.
"""
import asyncio

from database import posts_queries as q
from posting import post_publisher


# ── posts_queries CRUD ─────────────────────────────────────────────

def test_posts_crud_and_publication_upsert(db_conn):
    pid = q.create_post(db_conn, body="hello world", rating="mature",
                        image_alt="alt", now="2026-07-03 00:00:00")
    assert pid > 0
    p = q.get_post(db_conn, pid)
    assert p["body"] == "hello world" and p["rating"] == "mature"

    # Upsert twice on the same (post, platform, account) → one row, latest wins.
    q.upsert_post_publication(db_conn, post_id=pid, platform="bsky", account_id=0,
                              status="posted", external_url="https://x/1", now="t1")
    q.upsert_post_publication(db_conn, post_id=pid, platform="bsky", account_id=0,
                              status="failed", error="oops", now="t2")
    pubs = q.get_post_publications(db_conn, pid)
    assert len(pubs) == 1
    assert pubs[0]["status"] == "failed" and pubs[0]["error"] == "oops"

    # A different platform is a separate row.
    q.upsert_post_publication(db_conn, post_id=pid, platform="mast", account_id=0,
                              status="posted", now="t3")
    assert len(q.get_post_publications(db_conn, pid)) == 2

    lst = q.list_posts(db_conn)
    top = next(x for x in lst if x["post_id"] == pid)
    assert len(top["publications"]) == 2

    q.delete_post(db_conn, pid)
    assert q.get_post(db_conn, pid) is None
    assert q.get_post_publications(db_conn, pid) == []


def test_update_post_only_touches_allowed_fields(db_conn):
    pid = q.create_post(db_conn, body="draft", now="t0")
    # created_at is a real column but NOT in the allowed set → must be ignored.
    q.update_post(db_conn, pid, body="edited", rating="adult",
                  created_at="HACKED", now="t1")
    p = q.get_post(db_conn, pid)
    assert p["post_id"] == pid and p["body"] == "edited" and p["rating"] == "adult"
    assert p["created_at"] == "t0"


# ── publisher: pure/guard behaviour (no network) ───────────────────

def test_bsky_label_map():
    assert post_publisher._BSKY_LABELS["mature"] == ["sexual"]
    assert post_publisher._BSKY_LABELS["adult"] == ["porn"]
    assert post_publisher._BSKY_LABELS.get("general") is None


def test_publish_unsupported_platform_short_circuits():
    # A non-microblog platform (Pixiv) is not a post target → clear rejection.
    post = {"body": "hi", "rating": "general", "post_id": 1}
    res = asyncio.run(post_publisher._publish_one(post, "pix", None, {}))
    assert res["success"] is False and "isn't wired yet" in res["error"]


def test_publish_supported_platform_without_creds(db_conn):
    # No accounts configured + empty settings → clear "not connected" error, no network.
    post = {"body": "hi", "rating": "general", "post_id": 1}
    res_b = asyncio.run(post_publisher._publish_one(post, "bsky", None, {}))
    assert res_b["success"] is False and "isn't connected" in res_b["error"]
    res_m = asyncio.run(post_publisher._publish_one(post, "mast", None, {}))
    assert res_m["success"] is False and "isn't connected" in res_m["error"]


def test_publish_new_platforms_without_creds(db_conn):
    # Phase 3 platforms are now SUPPORTED but return a clear not-connected error.
    post = {"body": "hi", "rating": "general", "post_id": 1}
    cases = [
        ("thr", "Threads account isn't connected"),
        ("tw", "X/Twitter account isn't connected"),
        ("tum", "Tumblr posting needs OAuth1 tokens"),
    ]
    for plat, needle in cases:
        res = asyncio.run(post_publisher._publish_one(post, plat, None, {}))
        assert res["success"] is False and needle in res["error"], (plat, res)


def test_text_only_platform_rejects_an_attached_image():
    # Threads/Tumblr are text-only for now — an attached image is refused
    # BEFORE any credential/network work. (X gained image posting in 2.58.0.)
    post = {"body": "hi", "rating": "general", "post_id": 1, "image_path": "/tmp/x.png"}
    for plat in ("thr", "tum"):
        res = asyncio.run(post_publisher._publish_one(post, plat, None, {}))
        assert res["success"] is False and "text-only" in res["error"]


def test_x_accepts_an_image_now(db_conn):
    # X is no longer text-only: with an image but no creds it fails on the
    # connection check, NOT the text-only gate (proves the image path is open).
    pid = q.create_post(db_conn, body="hi", image_path="/tmp/x.png", now="t0")
    post = q.get_post(db_conn, pid)
    res = asyncio.run(post_publisher._publish_one(post, "tw", None, {}))
    assert res["success"] is False
    assert "text-only" not in res["error"]
    assert "connected" in res["error"].lower()


def test_post_media_round_trip_and_cascade_delete(db_conn):
    pid = q.create_post(db_conn, body="hi", now="t0")
    q.add_post_media(db_conn, post_id=pid, ordinal=0, path="/tmp/a.png", alt="a")
    q.add_post_media(db_conn, post_id=pid, ordinal=1, path="/tmp/b.png", alt="b")
    post = q.get_post(db_conn, pid)
    assert [m["path"] for m in post["media"]] == ["/tmp/a.png", "/tmp/b.png"]
    q.delete_post(db_conn, pid)
    assert q.get_post_media(db_conn, pid) == []


def test_publish_post_records_a_publication_row(db_conn):
    # End-to-end publisher path: create → publish (fails, not connected) →
    # a failed post_publications row is written (proves the DB record path).
    pid = q.create_post(db_conn, body="hello", now="t0")
    results = asyncio.run(post_publisher.publish_post(pid, ["bsky"], {}, {}))
    assert results and results[0]["success"] is False
    pubs = q.get_post_publications(db_conn, pid)
    assert len(pubs) == 1
    assert pubs[0]["platform"] == "bsky" and pubs[0]["status"] == "failed"
    assert "isn't connected" in pubs[0]["error"]


# ── handle-book (@mentions) — 2.61.0 ───────────────────────────────

def test_render_body_expands_bound_aliases_per_platform():
    mentions = [{
        "token": "luna",
        "handle_bsky": "luna.bsky.social", "handle_tw": "lunaX",
        "handle_mast": "luna@furry.social", "handle_thr": "", "handle_tum": "lunablog",
    }]
    body = "hey @luna nice art"
    assert post_publisher._render_body(body, mentions, "bsky") == "hey @luna.bsky.social nice art"
    assert post_publisher._render_body(body, mentions, "tw") == "hey @lunaX nice art"
    assert post_publisher._render_body(body, mentions, "mast") == "hey @luna@furry.social nice art"
    # No Threads handle for this contact → the alias stays plain text there.
    assert post_publisher._render_body(body, mentions, "thr") == "hey @luna nice art"


def test_render_body_whole_token_and_unbound():
    mentions = [{"token": "luna", "handle_tw": "lunaX"}]
    # @lunar must NOT be rewritten by the @luna binding (whole-token only).
    assert post_publisher._render_body("@luna and @lunar", mentions, "tw") == "@lunaX and @lunar"
    # An @alias with no binding is left exactly as typed.
    assert post_publisher._render_body("hi @bob", [], "tw") == "hi @bob"


def test_bsky_extract_tag_facets():
    from clients.bsky.client import BskyClient
    facets = BskyClient._extract_tag_facets("hi #Fox #1 #b2r!")
    tags = [f["features"][0]["tag"] for f in facets]
    assert tags == ["Fox", "b2r"]          # #1 (digit-first) is not a tag
    f0 = facets[0]
    assert f0["index"]["byteStart"] == 3 and f0["index"]["byteEnd"] == 7   # spans "#Fox"
    assert f0["features"][0]["$type"] == "app.bsky.richtext.facet#tag"
    # Trailing "!" is trimmed from the tag AND excluded from the byte range.
    assert facets[1]["features"][0]["tag"] == "b2r"


def test_contacts_and_post_mentions_round_trip(db_conn):
    cid = q.add_contact(db_conn, name="Luna", handle_bsky="@luna.bsky.social", handle_tw="lunaX")
    c = q.get_contact(db_conn, cid)
    assert c["name"] == "Luna"
    assert c["handle_bsky"] == "luna.bsky.social"     # leading @ stripped on save
    assert any(x["id"] == cid for x in q.list_contacts(db_conn))

    pid = q.create_post(db_conn, body="hi @luna", now="t0")
    q.set_post_mentions(db_conn, pid, [{"token": "@luna", "contact_id": cid}])
    men = q.get_post(db_conn, pid)["mentions"]
    assert len(men) == 1
    assert men[0]["token"] == "luna" and men[0]["contact_id"] == cid
    assert men[0]["handle_bsky"] == "luna.bsky.social"

    # Deleting the contact drops the binding (the alias reverts to plain text).
    q.delete_contact(db_conn, cid)
    assert q.get_contact(db_conn, cid) is None
    assert q.get_post(db_conn, pid)["mentions"] == []


def test_bsky_detect_handle_mentions():
    from clients.bsky.client import BskyClient
    got = BskyClient._detect_handle_mentions(
        "hi @alice.bsky.social and @bob.com! plus @luna and mail bob@example.com")
    # Full dotted handles are picked up; a bare @alias and an email's @domain are not.
    assert got == ["alice.bsky.social", "bob.com"]


def test_delete_post_clears_mentions(db_conn):
    cid = q.add_contact(db_conn, name="Rex")
    pid = q.create_post(db_conn, body="yo @rex", now="t0")
    q.set_post_mentions(db_conn, pid, [{"token": "rex", "contact_id": cid}])
    assert len(q.get_post_mentions(db_conn, pid)) == 1
    q.delete_post(db_conn, pid)
    assert q.get_post_mentions(db_conn, pid) == []
