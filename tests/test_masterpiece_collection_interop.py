"""Masterpieces Phase 6 (2.130.0) — a Masterpiece as a Collection member.

A Collection stays cross-type; adding a Masterpiece folds its WHOLE set of
site-uploads into the Collection's pooled stats / tags / personas / snapshot
pairs (spec §7). Driven off masterpiece_members — no artwork folder needed.
"""
import json

from database.db import get_connection
from database import collections_queries as cq
from database import masterpiece_queries as mq


def _seed(conn):
    conn.execute("INSERT INTO fa_submissions (submission_id, title, views, favorites_count, "
                 "comments_count, keywords) VALUES (100, 'Wolf', 50, 10, 2, ?)",
                 (json.dumps(["wolf"]),))
    conn.execute("INSERT INTO ws_submissions (submission_id, title, posted_at, views, "
                 "favorites_count, comments_count, keywords) VALUES (110, 'Wolf', '2026-01-01', 20, 4, 1, ?)",
                 (json.dumps(["art"]),))
    conn.commit()


def test_masterpiece_member_pools_into_collection():
    conn = get_connection()
    try:
        _seed(conn)
        mq.add_member(conn, "Wolf", "fa", "100", role="primary")
        mq.add_member(conn, "Wolf", "ws", "110")
        conn.commit()

        cid = cq.create_collection(conn, "Release bundle")
        cq.add_member(conn, cid, "masterpiece", "Wolf")
        conn.commit()

        roll = cq.rollup_collection(conn, cid)
        locs = {(l["platform"], l["submission_id"]) for l in roll["locations"]}
        assert locs == {("fa", "100"), ("ws", "110")}       # whole set folded in
        assert roll["totals"]["views"] == 70                # 50 + 20 pooled
        assert roll["totals"]["favorites"] == 14
        assert roll["totals"]["locations"] == 2
        assert set(roll["tags"]) == {"wolf", "art"}         # union across members
        # Each folded location is tagged with its source Masterpiece.
        assert all(l.get("masterpiece_name") == "Wolf" for l in roll["locations"])
    finally:
        conn.close()


def test_masterpiece_member_pairs_feed_snapshot_chart():
    conn = get_connection()
    try:
        _seed(conn)
        mq.add_member(conn, "Wolf", "fa", "100")
        mq.add_member(conn, "Wolf", "ws", "110")
        conn.commit()
        cid = cq.create_collection(conn, "Bundle")
        cq.add_member(conn, cid, "masterpiece", "Wolf")
        conn.commit()

        pairs = set(cq.collection_member_pairs(conn, cid))
        assert pairs == {("fa", "100"), ("ws", "110")}
    finally:
        conn.close()


def test_masterpiece_members_excluded_from_suggestions():
    """A submission already inside a collection (via a Masterpiece member) must not
    be re-proposed by the auto-suggest engine."""
    conn = get_connection()
    try:
        _seed(conn)
        mq.add_member(conn, "Wolf", "fa", "100")
        conn.commit()
        cid = cq.create_collection(conn, "Bundle")
        cq.add_member(conn, cid, "masterpiece", "Wolf")
        conn.commit()

        collected = cq._collected_pairs(conn)
        assert ("fa", "100") in collected
    finally:
        conn.close()
