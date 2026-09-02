"""Masterpieces Phase 1 (2.125.0) — membership CRUD + the cross-site rollup.

A Masterpiece pools the SAME image posted to N sites. These tests seed real
per-platform submissions, attach them as members, and assert the pooled totals /
tags / personas resolve the same way a Collection's do (the rollup reuses
collections_queries' per-platform stat normalisation).
"""
import json

from database.db import get_connection
from database import masterpiece_queries as mq


def _seed(conn):
    """The same image on FA (#100) and Weasyl (#110)."""
    conn.execute("INSERT INTO fa_submissions (submission_id, title, views, favorites_count, "
                 "comments_count, keywords, thumbnail_url) "
                 "VALUES (100, 'Wolf', 50, 10, 2, ?, 'http://cdn/a.jpg')",
                 (json.dumps(["wolf", "canine"]),))
    conn.execute("INSERT INTO ws_submissions (submission_id, title, posted_at, views, "
                 "favorites_count, comments_count, keywords) "
                 "VALUES (110, 'Wolf', '2026-01-01', 20, 4, 1, ?)",
                 (json.dumps(["wolf", "art"]),))
    conn.commit()


# ── Membership CRUD + index adoption ─────────────────────────────

def test_add_indexes_name_and_pairs_resolve():
    conn = get_connection()
    try:
        _seed(conn)
        mq.add_member(conn, "Wolf", "fa", "100", role="primary")
        conn.commit()
        # add_member adopts the name into the thin index.
        assert conn.execute("SELECT 1 FROM masterpieces WHERE name = 'Wolf'").fetchone()
        assert mq.member_pairs(conn, "Wolf") == [("fa", "100")]
    finally:
        conn.close()


def test_add_is_idempotent_on_pk():
    conn = get_connection()
    try:
        _seed(conn)
        mq.add_member(conn, "Wolf", "fa", "100")
        mq.add_member(conn, "Wolf", "fa", "100", linked_via="phash")  # same PK
        conn.commit()
        assert len(mq.get_members(conn, "Wolf")) == 1
    finally:
        conn.close()


def test_remove_member():
    conn = get_connection()
    try:
        _seed(conn)
        mq.add_member(conn, "Wolf", "fa", "100")
        mq.add_member(conn, "Wolf", "ws", "110")
        mq.remove_member(conn, "Wolf", "fa", "100")
        conn.commit()
        assert mq.member_pairs(conn, "Wolf") == [("ws", "110")]
    finally:
        conn.close()


# ── Rollup pooling ───────────────────────────────────────────────

def test_rollup_pools_member_stats_and_tags():
    conn = get_connection()
    try:
        _seed(conn)
        mq.add_member(conn, "Wolf", "fa", "100", role="primary", linked_via="manual")
        mq.add_member(conn, "Wolf", "ws", "110", linked_via="phash")
        conn.commit()

        roll = mq.rollup_members(conn, "Wolf")
        assert roll["totals"]["views"] == 70          # 50 + 20
        assert roll["totals"]["favorites"] == 14      # 10 + 4
        assert roll["totals"]["comments"] == 3        # 2 + 1
        assert roll["totals"]["platforms"] == 2
        assert roll["totals"]["locations"] == 2
        assert set(roll["tags"]) == {"wolf", "canine", "art"}   # union
        assert len(roll["members"]) == 2
        # role / linked_via carried onto the resolved location.
        fa_loc = next(l for l in roll["locations"] if l["platform"] == "fa")
        assert fa_loc["role"] == "primary" and fa_loc["linked_via"] == "manual"
    finally:
        conn.close()


def test_rollup_empty_for_unmembered_name():
    conn = get_connection()
    try:
        roll = mq.rollup_members(conn, "Nonexistent")
        assert roll["members"] == [] and roll["locations"] == []
        assert roll["totals"] == {"views": 0, "favorites": 0, "comments": 0,
                                  "platforms": 0, "locations": 0}
        assert roll["tags"] == [] and roll["persona_ids"] == []
    finally:
        conn.close()


def test_rollup_persona_from_account():
    conn = get_connection()
    try:
        _seed(conn)
        # Map an FA account to persona 7; the member carries that account_id.
        # (_acct_to_persona reads accounts.persona_id directly — no personas row
        # or FK needed for the rollup to span personas.)
        conn.execute("INSERT INTO accounts (account_id, platform, label, persona_id) "
                     "VALUES (3, 'fa', 'main', 7)")
        conn.commit()
        mq.add_member(conn, "Wolf", "fa", "100", account_id=3)
        conn.commit()
        roll = mq.rollup_members(conn, "Wolf")
        assert roll["persona_ids"] == [7]
    finally:
        conn.close()


# ── Light summary (Library grid) ─────────────────────────────────

def test_summarize_cover_and_counts():
    conn = get_connection()
    try:
        _seed(conn)
        mq.add_member(conn, "Wolf", "fa", "100")   # has thumbnail_url
        mq.add_member(conn, "Wolf", "ws", "110")   # no thumbnail_url
        conn.commit()
        s = mq.summarize(conn, "Wolf")
        assert s["member_count"] == 2
        assert s["platforms"] == ["fa", "ws"]
        assert s["totals"]["views"] == 70
        assert s["cover_thumb"] == "http://cdn/a.jpg"   # first member with an image
        assert s["cover_platform"] == "fa"
    finally:
        conn.close()
