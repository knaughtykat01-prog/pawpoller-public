"""Perf guardrails (2.165.0) — batched rollups for the list endpoints.

The Masterpieces grid used to pay one submission query PER MEMBER and one write
PER NAME on every load ("live rollup × N"); /api/works paid one stat query PER
PUBLICATION. These tests lock in two things for the batched replacements:

  1. EQUIVALENCE — the batched result is byte-for-byte the same as the per-item
     code it replaces (so this is a pure speedup, not a behaviour change).
  2. QUERY COUNT — the submission-table fan-out is now bounded by the number of
     PLATFORMS, not the number of members/publications.

Query counting uses sqlite3's trace callback, which fires once per executed
statement.
"""
import json
import re

from database.db import get_connection
from database import masterpiece_queries as mq
from database import posting_queries
from database import collections_queries as cq


_SUB_TABLES = set(cq._TABLE_MAP.values())


def _submission_selects(statements):
    """The subset of traced statements that SELECT from a per-platform
    submission table — the fan-out we're trying to bound."""
    out = []
    for s in statements:
        if not s.lstrip().upper().startswith("SELECT"):
            continue
        if any(re.search(r"\bFROM\s+" + re.escape(t) + r"\b", s) for t in _SUB_TABLES):
            out.append(s)
    return out


def _trace(conn, fn):
    seen = []
    conn.set_trace_callback(seen.append)
    try:
        result = fn()
    finally:
        conn.set_trace_callback(None)
    return result, seen


def _seed_submissions(conn):
    """The same image on FA (#100) and Weasyl (#110), plus a second FA piece."""
    conn.execute("INSERT INTO fa_submissions (submission_id, title, views, favorites_count, "
                 "comments_count, keywords, thumbnail_url) "
                 "VALUES (100, 'Wolf', 50, 10, 2, ?, 'http://cdn/a.jpg')",
                 (json.dumps(["wolf", "canine"]),))
    conn.execute("INSERT INTO fa_submissions (submission_id, title, views, favorites_count, "
                 "comments_count, keywords, thumbnail_url) "
                 "VALUES (200, 'Fox', 5, 1, 0, ?, 'http://cdn/f.jpg')",
                 (json.dumps(["fox"]),))
    conn.execute("INSERT INTO ws_submissions (submission_id, title, posted_at, views, "
                 "favorites_count, comments_count, keywords) "
                 "VALUES (110, 'Wolf', '2026-01-01', 20, 4, 1, ?)",
                 (json.dumps(["wolf", "art"]),))
    conn.commit()


# ── summarize_many equivalence + query bound ─────────────────────

def test_summarize_many_matches_per_item():
    conn = get_connection()
    try:
        _seed_submissions(conn)
        mq.add_member(conn, "Wolf", "fa", "100", role="primary", account_id=3)
        mq.add_member(conn, "Wolf", "ws", "110", linked_via="phash")
        mq.add_member(conn, "Fox", "fa", "200")
        mq.ensure_indexed(conn, "Empty")           # indexed, no members
        conn.execute("INSERT INTO accounts (account_id, platform, label, persona_id) "
                     "VALUES (3, 'fa', 'main', 7)")
        conn.commit()

        names = ["Wolf", "Fox", "Empty"]
        batched = mq.summarize_many(conn, names)
        per_item = {n: mq.summarize(conn, n) for n in names}
        assert batched == per_item

        # Spot-check the actual content so a shared bug in both can't pass silently.
        assert batched["Wolf"]["totals"]["views"] == 70
        assert batched["Wolf"]["persona_ids"] == [7]
        assert batched["Wolf"]["cover_thumb"] == "http://cdn/a.jpg"
        assert batched["Empty"]["member_count"] == 0
    finally:
        conn.close()


def test_summarize_many_bounds_submission_queries_by_platform():
    conn = get_connection()
    try:
        _seed_submissions(conn)
        # 3 members across 2 platforms (fa, ws).
        mq.add_member(conn, "Wolf", "fa", "100")
        mq.add_member(conn, "Wolf", "ws", "110")
        mq.add_member(conn, "Fox", "fa", "200")
        conn.commit()

        _, seen = _trace(conn, lambda: mq.summarize_many(conn, ["Wolf", "Fox"]))
        # One submission-table SELECT per platform present (fa, ws) — NOT one per
        # member. The whole grid's rollup is O(platforms), not O(members).
        assert len(_submission_selects(seen)) == 2

        # Contrast: the per-item path issues one submission query per member (3).
        _, seen_old = _trace(
            conn, lambda: [mq.summarize(conn, n) for n in ["Wolf", "Fox"]])
        assert len(_submission_selects(seen_old)) == 3
    finally:
        conn.close()


# ── ensure_indexed_bulk ──────────────────────────────────────────

def test_ensure_indexed_bulk_only_inserts_missing():
    conn = get_connection()
    try:
        mq.ensure_indexed(conn, "Already")
        conn.commit()
        inserted = mq.ensure_indexed_bulk(conn, ["Already", "New1", "New2"])
        conn.commit()
        assert inserted == 2
        rows = {r[0] for r in conn.execute("SELECT name FROM masterpieces").fetchall()}
        assert {"Already", "New1", "New2"} <= rows
        # Re-running is a no-op (0 writes worth of names).
        assert mq.ensure_indexed_bulk(conn, ["Already", "New1", "New2"]) == 0
    finally:
        conn.close()


# ── get_publications_with_stats batching ─────────────────────────

def test_publications_stats_batched_matches_and_bounds_queries():
    conn = get_connection()
    try:
        _seed_submissions(conn)
        posting_queries.upsert_publication(
            conn, "Wolf", 0, "fa", content_type="artwork",
            external_id="100", status="posted")
        posting_queries.upsert_publication(
            conn, "Wolf", 0, "ws", content_type="artwork",
            external_id="110", status="posted")
        posting_queries.upsert_publication(
            conn, "Fox", 0, "fa", content_type="artwork",
            external_id="200", status="posted")

        enriched, seen = _trace(
            conn, lambda: posting_queries.get_publications_with_stats(
                conn, content_type="artwork"))

        by = {(p["platform"], p["external_id"]): p for p in enriched}
        assert by[("fa", "100")]["stats"]["views"] == 50
        assert by[("ws", "110")]["stats"]["favorites_count"] == 4
        assert by[("fa", "200")]["stats"]["comments_count"] == 0

        # 3 publications across 2 platforms → 2 stat queries, not 3.
        assert len(_submission_selects(seen)) == 2
    finally:
        conn.close()


def test_publications_stats_missing_row_is_none():
    conn = get_connection()
    try:
        posting_queries.upsert_publication(
            conn, "Ghost", 0, "fa", content_type="artwork",
            external_id="999", status="posted")   # no fa_submissions row
        enriched = posting_queries.get_publications_with_stats(conn, content_type="artwork")
        assert enriched[0]["stats"] is None
    finally:
        conn.close()
