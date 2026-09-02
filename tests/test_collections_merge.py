"""Phase 3 — the Cross-Platform → Collections merge (2.113.0).

Covers the reusable combined-snapshot core, collection member-pair resolution,
collection-aware suggestions, and the one-time link→collection migration
(idempotent + reversible).
"""
import json

from database.db import get_connection
from database import collections_queries as cq
from database import analytics_queries as aq
from database import posting_queries


def _seed_two_platform_piece(conn):
    """The same piece on FA (#100) and Weasyl (#110), plus a tweet (#200)."""
    conn.execute("INSERT INTO fa_submissions (submission_id, title, views, favorites_count, "
                 "comments_count, keywords) VALUES (100, 'Wolf Tale', 50, 10, 2, ?)",
                 (json.dumps(["wolf"]),))
    conn.execute("INSERT INTO ws_submissions (submission_id, title, posted_at, views, "
                 "favorites_count, comments_count) VALUES (110, 'Wolf Tale', '2026-01-01', 20, 4, 1)")
    conn.execute("INSERT INTO tw_submissions (submission_id, title, views, likes, replies) "
                 "VALUES ('200', 'announcing Wolf Tale', 30, 5, 1)")
    conn.commit()


# ── Combined snapshots (the reusable core) ───────────────────────────────

def test_get_combined_snapshots_merges_by_timestamp():
    conn = get_connection()
    try:
        _seed_two_platform_piece(conn)  # parent rows for the snapshot FKs
        # FA + a tweet share one timestamp (values sum) and FA has a second point alone.
        conn.execute("INSERT INTO fa_snapshots (submission_id, polled_at, views, favorites_count, comments_count) "
                     "VALUES (100, '2026-01-01 00:00', 10, 2, 1)")
        conn.execute("INSERT INTO tw_snapshots (submission_id, polled_at, views, likes, replies) "
                     "VALUES ('200', '2026-01-01 00:00', 5, 3, 0)")
        conn.execute("INSERT INTO fa_snapshots (submission_id, polled_at, views, favorites_count, comments_count) "
                     "VALUES (100, '2026-01-02 00:00', 20, 4, 2)")
        conn.commit()

        series = aq.get_combined_snapshots(conn, [("fa", "100"), ("tw", "200")])
        assert [s["polled_at"] for s in series] == ["2026-01-01 00:00", "2026-01-02 00:00"]
        # tw likes/replies map onto the canonical faves/comments keys. `score`
        # rides in its own series (0 here — neither platform is score-model).
        assert series[0] == {"polled_at": "2026-01-01 00:00", "views": 15,
                             "score": 0, "favorites_count": 5, "comments_count": 1}
        assert series[1]["views"] == 20 and series[1]["favorites_count"] == 4
    finally:
        conn.close()


def test_get_combined_snapshots_keeps_booru_score_out_of_views():
    """e621/Furbooru report a net up−down score, not views. Merging a booru
    submission into a combined series must not inflate the view line — the
    per-call metric map used to put `score` in the views slot."""
    conn = get_connection()
    try:
        _seed_two_platform_piece(conn)
        conn.execute("INSERT INTO fa_snapshots (submission_id, polled_at, views, favorites_count, comments_count) "
                     "VALUES (100, '2026-01-01 00:00', 10, 2, 1)")
        conn.execute("INSERT INTO e621_submissions (submission_id, title) VALUES ('300', 'Art')")
        conn.execute("INSERT INTO e621_snapshots (submission_id, polled_at, score, up_score,"
                     " down_score, favorites_count, comments_count) "
                     "VALUES ('300', '2026-01-01 00:00', 63, 66, -3, 161, 0)")
        conn.commit()

        series = aq.get_combined_snapshots(conn, [("fa", "100"), ("e621", "300")])
        assert len(series) == 1
        assert series[0]["views"] == 10          # NOT 73
        assert series[0]["score"] == 63
        assert series[0]["favorites_count"] == 163
    finally:
        conn.close()


def test_get_link_combined_snapshots_still_works():
    """Regression: the refactor keeps the link wrapper working."""
    conn = get_connection()
    try:
        _seed_two_platform_piece(conn)  # parent rows for the snapshot FKs
        conn.execute("INSERT INTO fa_snapshots (submission_id, polled_at, views, favorites_count, comments_count) "
                     "VALUES (100, '2026-01-01 00:00', 7, 1, 0)")
        link_id = aq.create_link(conn, [{"platform": "fa", "submission_id": 100},
                                        {"platform": "ws", "submission_id": 110}])
        series = aq.get_link_combined_snapshots(conn, link_id)
        assert len(series) == 1 and series[0]["views"] == 7
    finally:
        conn.close()


# ── Collection member pairs ──────────────────────────────────────────────

def test_collection_member_pairs_resolves_work_and_submission_excludes_post():
    conn = get_connection()
    try:
        _seed_two_platform_piece(conn)
        # The FA piece is also a managed work (publication work -> submission 100).
        posting_queries.upsert_publication(
            conn, story_name="Wolf_Tale", chapter_index=0, platform="fa", account_id=2,
            content_type="artwork", external_id="100", external_url="https://fa/100", status="posted")
        cid = cq.create_collection(conn, "Wolf Tale — everywhere")
        cq.add_member(conn, cid, "work", "artwork:Wolf_Tale")     # -> ("fa","100")
        cq.add_member(conn, cid, "submission", "tw:200")           # -> ("tw","200")
        cq.add_member(conn, cid, "post", "5")                      # excluded (no stats table)
        conn.commit()

        pairs = set(cq.collection_member_pairs(conn, cid))
        assert pairs == {("fa", "100"), ("tw", "200")}
    finally:
        conn.close()


# ── Collection-aware suggestions ─────────────────────────────────────────

def test_auto_suggest_collections_finds_then_excludes_when_collected():
    conn = get_connection()
    try:
        _seed_two_platform_piece(conn)  # FA #100 + WS #110 share the title "Wolf Tale"

        sugg = cq.auto_suggest_collections(conn)
        # The identical-title FA/WS pair should surface (tw title differs, excluded).
        pairs = {(m["platform"], str(m["submission_id"]))
                 for s in sugg for m in s["submissions"]}
        assert ("fa", "100") in pairs and ("ws", "110") in pairs

        # Once both are inside a collection, the pair is no longer suggested.
        cid = cq.create_collection(conn, "Wolf Tale")
        cq.add_member(conn, cid, "submission", "fa:100")
        cq.add_member(conn, cid, "submission", "ws:110")
        conn.commit()
        sugg2 = cq.auto_suggest_collections(conn)
        pairs2 = {(m["platform"], str(m["submission_id"]))
                  for s in sugg2 for m in s["submissions"]}
        assert ("fa", "100") not in pairs2 and ("ws", "110") not in pairs2
    finally:
        conn.close()


# ── Link → Collection migration ──────────────────────────────────────────

def test_migrate_links_to_collections_idempotent_and_reversible():
    conn = get_connection()
    try:
        _seed_two_platform_piece(conn)
        # A cross-platform link joining FA #100 + WS #110.
        link_id = aq.create_link(conn, [{"platform": "fa", "submission_id": 100},
                                        {"platform": "ws", "submission_id": 110}])

        n = cq.migrate_links_to_collections(conn)
        conn.commit()
        assert n == 1

        # One collection, provenance-stamped, named from the first resolvable title.
        row = conn.execute(
            "SELECT id, name, source_link_id FROM collections WHERE source_link_id = ?",
            (link_id,)).fetchone()
        assert row is not None
        assert row["name"] == "Wolf Tale"
        members = {m["member_ref"] for m in cq.get_members(conn, row["id"])}
        assert members == {"fa:100", "ws:110"}

        # Idempotent: a second run creates nothing new.
        assert cq.migrate_links_to_collections(conn) == 0
        conn.commit()
        assert conn.execute("SELECT COUNT(*) c FROM collections WHERE source_link_id = ?",
                            (link_id,)).fetchone()["c"] == 1

        # Reversible: the original link rows are left intact.
        assert conn.execute("SELECT COUNT(*) c FROM submission_links").fetchone()["c"] == 1
        assert conn.execute("SELECT COUNT(*) c FROM submission_link_members "
                            "WHERE link_id = ?", (link_id,)).fetchone()["c"] == 2
    finally:
        conn.close()


def test_locations_carry_thumbnail_and_summary_auto_covers():
    """Phase 5: each location exposes its image + the hub summary auto-covers from
    the first location that has one, so Collections can SHOW the art."""
    conn = get_connection()
    try:
        conn.execute("INSERT INTO fa_submissions (submission_id, title, views, thumbnail_url) "
                     "VALUES (100, 'Wolf Tale', 50, 'https://d.facdn.net/a.jpg')")
        cid = cq.create_collection(conn, "Wolf Tale")
        cq.add_member(conn, cid, "submission", "fa:100")
        conn.commit()

        roll = cq.rollup_collection(conn, cid)
        assert roll["locations"][0]["thumbnail_url"] == "https://d.facdn.net/a.jpg"

        summ = {c["id"]: c for c in cq.list_collections_with_summary(conn)}[cid]
        assert summ["cover_thumb"] == "https://d.facdn.net/a.jpg"
        assert summ["cover_platform"] == "fa"
    finally:
        conn.close()


def test_migrate_skips_degenerate_single_member_link():
    conn = get_connection()
    try:
        _seed_two_platform_piece(conn)
        cur = conn.execute("INSERT INTO submission_links DEFAULT VALUES")
        conn.execute("INSERT INTO submission_link_members (link_id, platform, submission_id) "
                     "VALUES (?, 'fa', 100)", (cur.lastrowid,))
        conn.commit()
        assert cq.migrate_links_to_collections(conn) == 0
    finally:
        conn.close()
