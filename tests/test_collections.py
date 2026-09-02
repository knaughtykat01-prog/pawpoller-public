"""Collections: CRUD + the live rollup that resolves polymorphic members into
pooled analytics / locations / tags / personas / companion story.
"""
import json

from database.db import get_connection
from database import collections_queries as cq
from database import posting_queries


def _seed(conn):
    # Two accounts under two personas.
    conn.execute("INSERT INTO accounts (account_id, platform, persona_id, label, enabled) "
                 "VALUES (2, 'fa', 1, 'GalleryPersona', 1)")
    conn.execute("INSERT INTO accounts (account_id, platform, persona_id, label, enabled) "
                 "VALUES (13, 'tw', 4, 'MicroblogPersona', 1)")
    # An FA gallery submission (views/faves/comments) + a tweet (views/likes/replies).
    conn.execute("INSERT INTO fa_submissions (submission_id, account_id, title, views, "
                 "favorites_count, comments_count, keywords) VALUES (100, 2, 'My Piece', 50, 10, 2, ?)",
                 (json.dumps(["wolf", "anthro"]),))
    conn.execute("INSERT INTO tw_submissions (submission_id, account_id, title, views, likes, "
                 "replies, keywords) VALUES ('200', 13, 'announcing my art', 30, 5, 1, ?)",
                 (json.dumps(["fox"]),))
    # The FA piece is also a managed work (a publication linking work -> submission 100).
    posting_queries.upsert_publication(
        conn, story_name="My_Piece", chapter_index=0, platform="fa", account_id=2,
        content_type="artwork", external_id="100", external_url="https://fa/100", status="posted")
    conn.commit()


def test_crud_and_members():
    conn = get_connection()
    try:
        cid = cq.create_collection(conn, "Test Coll", notes="hi")
        assert cid
        cq.add_member(conn, cid, "submission", "tw:200", role="announcement")
        cq.add_member(conn, cid, "submission", "tw:200")  # dupe -> IGNORE
        conn.commit()
        assert len(cq.get_members(conn, cid)) == 1
        cq.remove_member(conn, cid, "submission", "tw:200")
        conn.commit()
        assert cq.get_members(conn, cid) == []
        cq.delete_collection(conn, cid)
        conn.commit()
        assert cq.get_collection(conn, cid) is None
    finally:
        conn.close()


def test_rollup_pools_across_work_and_submission():
    conn = get_connection()
    try:
        _seed(conn)
        cid = cq.create_collection(conn, "My Piece — everywhere")
        cq.add_member(conn, cid, "work", "artwork:My_Piece", role="art")     # -> resolves FA pub
        cq.add_member(conn, cid, "submission", "tw:200", role="announcement")  # -> the tweet
        cq.add_member(conn, cid, "work", "story:My_Story", role="story")       # companion story
        conn.commit()

        roll = cq.rollup_collection(conn, cid)
        assert roll is not None
        # Two resolvable locations (FA from the work + the tweet); story has no pubs.
        plats = sorted(l["platform"] for l in roll["locations"])
        assert plats == ["fa", "tw"]
        # Pooled totals: views 50+30, faves 10+5, comments 2+1.
        assert roll["totals"]["views"] == 80
        assert roll["totals"]["favorites"] == 15
        assert roll["totals"]["comments"] == 3
        assert roll["totals"]["platforms"] == 2
        # Merged tags across both.
        assert set(roll["tags"]) == {"wolf", "anthro", "fox"}
        # Personas spanned (FA acct 2 -> persona 1, tw acct 13 -> persona 4).
        assert roll["persona_ids"] == [1, 4]
        # Companion story surfaced.
        assert roll["story"] == {"name": "My_Story"}
    finally:
        conn.close()


def test_list_summary():
    conn = get_connection()
    try:
        _seed(conn)
        cid = cq.create_collection(conn, "C1")
        cq.add_member(conn, cid, "submission", "fa:100")
        conn.commit()
        rows = cq.list_collections_with_summary(conn)
        row = next(r for r in rows if r["id"] == cid)
        assert row["totals"]["views"] == 50
        assert row["member_count"] == 1
        assert row["platforms"] == ["fa"]
    finally:
        conn.close()
