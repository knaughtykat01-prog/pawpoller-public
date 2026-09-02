"""Gap wave 3: persona defaults · insights · threads (G8)."""
import asyncio

from database.db import get_connection
from database import personas, posts_queries, analytics_queries


def test_persona_defaults_roundtrip_and_manifest():
    conn = get_connection()
    try:
        pid = personas.create_persona(conn, "Testa")
        personas.update_persona(conn, pid, default_platforms="ib,fa",
                                default_rating="adult", preferred_post_time="20:00")
        p = personas.get_persona(conn, pid)
        assert (p["default_platforms"], p["preferred_post_time"]) == ("ib,fa", "20:00")
        man = personas.get_manifest(conn)
        assert man[0]["default_rating"] == "adult"
        # Old-client manifest (no default keys) still applies cleanly.
        assert personas.apply_manifest(conn, [{"persona_id": 99, "name": "Old"}]) == 1
        assert personas.get_persona(conn, 99)["default_platforms"] == ""
    finally:
        conn.close()


def test_insights_buckets_and_medians():
    conn = get_connection()
    try:
        # 5 IB pieces: 4 typical + one 3× overperformer, all Tue 20:00 UTC.
        for i, v in enumerate([100, 100, 100, 100, 300]):
            conn.execute("INSERT INTO submissions (submission_id, title, views,"
                         " create_datetime) VALUES (?, ?, ?, '2026-07-21 20:00:00')",
                         (i + 1, f"P{i}", v))
        conn.commit()
        ins = analytics_queries.get_posting_insights(conn, tz_offset_minutes=0)
    finally:
        conn.close()
    assert ins["platforms"]["ib"] == {"metric": "views", "count": 5, "median": 100}
    assert ins["overperformers"][0]["ratio"] == 3.0
    assert ins["weekday"][1]["count"] == 5          # Tuesday
    assert ins["hour"][20]["count"] == 5
    assert ins["hour"][20]["median"] == 1.0         # relative engagement


def test_thread_parts_storage_and_feed():
    conn = get_connection()
    try:
        parent = posts_queries.create_post(conn, body="part 1", now="2026-07-24")
        for i, t in enumerate(["part 2", "part 3"]):
            posts_queries.create_post(conn, body=t, now="2026-07-24",
                                      parent_post_id=parent, thread_ordinal=i + 1)
        feed = posts_queries.list_posts(conn)
        parts = posts_queries.get_thread_parts(conn, parent)
    finally:
        conn.close()
    assert len(feed) == 1 and feed[0]["thread_count"] == 2   # children hidden
    assert [p["body"] for p in parts] == ["part 2", "part 3"]


def test_bsky_chain_carries_refs(monkeypatch):
    """The core correctness property: part N replies to part N-1, root = part 1."""
    from posting import post_publisher
    calls = []

    class FakeBsky:
        def __init__(self, **kw): pass
        async def create_post(self, text, *, reply=None, **kw):
            calls.append(reply)
            return {"uri": f"at://p{len(calls)}", "cid": f"c{len(calls)}",
                    "url": f"https://x/{len(calls)}"}
        async def close(self): pass

    import clients.bsky.client as bc
    monkeypatch.setattr(bc, "BskyClient", FakeBsky)
    monkeypatch.setattr(post_publisher, "_resolve_creds",
                        lambda plat, aid, st: (1, {"bsky_identifier": "i",
                                                   "bsky_app_password": "p"}))
    parts = [{"post_id": 11, "body": "two"}, {"post_id": 12, "body": "three"}]
    parent = {"external_id": "at://root", "_refs": {"uri": "at://root", "cid": "cr"}}
    out = asyncio.run(post_publisher._publish_thread_parts(parts, "bsky", None, None, parent))
    assert [o["success"] for o in out] == [True, True]
    assert calls[0] == {"root": {"uri": "at://root", "cid": "cr"},
                        "parent": {"uri": "at://root", "cid": "cr"}}
    assert calls[1]["parent"] == {"uri": "at://p1", "cid": "c1"}   # chained
    assert calls[1]["root"] == {"uri": "at://root", "cid": "cr"}   # rooted
