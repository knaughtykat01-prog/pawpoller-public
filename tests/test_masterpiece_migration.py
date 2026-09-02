"""Masterpieces Phase 7 (2.131.0) — submission_links → Masterpieces migration +
the auto-suggest engine re-pointing (same-image → Masterpiece, same-piece →
Collection). No artwork folder needed — driven off the DB tables.
"""
from database.db import get_connection
from database import masterpiece_queries as mq
from database import collections_queries as cq
from database import analytics_queries as aq
from database import image_hash


def _seed(conn):
    conn.execute("INSERT INTO fa_submissions (submission_id, title, account_id) VALUES (100, 'Wolf', 3)")
    conn.execute("INSERT INTO ws_submissions (submission_id, title) VALUES (110, 'Wolf')")
    conn.commit()


# ── Migration ────────────────────────────────────────────────────

def test_migrate_links_to_masterpieces_idempotent_reversible():
    conn = get_connection()
    try:
        _seed(conn)
        link_id = aq.create_link(conn, [{"platform": "fa", "submission_id": 100},
                                        {"platform": "ws", "submission_id": 110}])

        n = mq.migrate_links_to_masterpieces(conn)
        conn.commit()
        assert n == 1

        row = conn.execute(
            "SELECT name, source_link_id FROM masterpieces WHERE source_link_id = ?",
            (link_id,)).fetchone()
        assert row is not None
        name = row["name"]
        assert name == "Wolf"                    # named from the first resolvable title

        members = mq.get_members(conn, name)
        assert {(m["platform"], m["submission_id"]) for m in members} == {("fa", "100"), ("ws", "110")}
        assert all(m["linked_via"] == "migration" for m in members)
        primary = next(m for m in members if m["role"] == "primary")
        assert primary["platform"] == "fa" and primary["account_id"] == 3   # persona carried

        # Idempotent: a second run creates nothing new.
        assert mq.migrate_links_to_masterpieces(conn) == 0
        conn.commit()
        assert conn.execute("SELECT COUNT(*) c FROM masterpieces WHERE source_link_id = ?",
                            (link_id,)).fetchone()["c"] == 1

        # Reversible: the original link rows are left intact.
        assert conn.execute("SELECT COUNT(*) c FROM submission_links").fetchone()["c"] == 1
        assert conn.execute("SELECT COUNT(*) c FROM submission_link_members WHERE link_id = ?",
                            (link_id,)).fetchone()["c"] == 2
    finally:
        conn.close()


def test_migrate_skips_single_member_links():
    conn = get_connection()
    try:
        _seed(conn)
        aq.create_link(conn, [{"platform": "fa", "submission_id": 100}])   # only 1 member
        assert mq.migrate_links_to_masterpieces(conn) == 0
    finally:
        conn.close()


def test_migrate_noop_without_links():
    conn = get_connection()
    try:
        assert mq.migrate_links_to_masterpieces(conn) == 0
    finally:
        conn.close()


# ── Auto-suggest re-pointing (§7) ────────────────────────────────

def test_auto_suggest_targets_masterpiece_for_same_image():
    conn = get_connection()
    try:
        # Two DIFFERENT-titled submissions (no title match) that are the SAME image.
        conn.execute("INSERT INTO fa_submissions (submission_id, title) VALUES (100, 'Alpha')")
        conn.execute("INSERT INTO ws_submissions (submission_id, title) VALUES (110, 'Beta')")
        image_hash.ensure_table(conn)
        image_hash.store(conn, "fa", "100", "abcabcabcabcabca")
        image_hash.store(conn, "ws", "110", "abcabcabcabcabca")   # identical hash → same image
        conn.commit()

        sug = cq.auto_suggest_collections(conn)
        image = [s for s in sug if s.get("reason") == "image"]
        assert image, "expected an image-based suggestion"
        assert all(s["target"] == "masterpiece" for s in image)   # same-image → Masterpiece
    finally:
        conn.close()


def test_auto_suggest_targets_collection_for_same_title():
    conn = get_connection()
    try:
        # Same title across platforms, NO image hashes → a title (same-piece) match.
        conn.execute("INSERT INTO fa_submissions (submission_id, title) VALUES (100, 'Shared Title Piece')")
        conn.execute("INSERT INTO ws_submissions (submission_id, title) VALUES (110, 'Shared Title Piece')")
        conn.commit()

        sug = cq.auto_suggest_collections(conn)
        title = [s for s in sug if s.get("reason") == "title"]
        assert title, "expected a title-based suggestion"
        assert all(s["target"] == "collection" for s in title)    # same-piece → Collection
    finally:
        conn.close()
