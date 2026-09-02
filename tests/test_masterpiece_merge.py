"""Masterpiece de-duplication (2.144.0): merge + perceptual-hash grouping.

merge_masterpieces folds one Masterpiece's members into another and removes the
duplicate; duplicate_masterpiece_groups clusters hero-image hashes by Hamming
distance so the UI can offer look-alikes to merge.
"""
from database.db import get_connection
from database import masterpiece_queries as mq, image_hash


def test_merge_moves_members_and_removes_drop():
    conn = get_connection()
    mq.add_member(conn, "Keep", "fa", "111")
    mq.add_member(conn, "Drop", "bsky", "222")
    mq.add_member(conn, "Drop", "tw", "333")
    conn.commit()
    moved = mq.merge_masterpieces(conn, "Keep", "Drop")
    assert moved == 2
    assert sorted(mq.member_pairs(conn, "Keep")) == [("bsky", "222"), ("fa", "111"), ("tw", "333")]
    assert mq.member_pairs(conn, "Drop") == []
    # index row for Drop is gone
    assert conn.execute("SELECT COUNT(*) FROM masterpieces WHERE name = 'Drop'").fetchone()[0] == 0
    conn.close()


def test_merge_dedupes_colliding_members():
    conn = get_connection()
    mq.add_member(conn, "Keep", "fa", "111")
    mq.add_member(conn, "Drop", "fa", "111")   # same upload already on Keep
    mq.add_member(conn, "Drop", "bsky", "222")
    conn.commit()
    moved = mq.merge_masterpieces(conn, "Keep", "Drop")
    assert moved == 1                          # only the bsky one is new
    assert sorted(mq.member_pairs(conn, "Keep")) == [("bsky", "222"), ("fa", "111")]
    conn.close()


def test_duplicate_groups_cluster_by_hamming():
    conn = get_connection()
    image_hash.ensure_table(conn)
    image_hash.store(conn, "__mp__", "A", "ffffffffffffffff")
    image_hash.store(conn, "__mp__", "B", "ffffffffffffffff")   # identical → same group as A
    image_hash.store(conn, "__mp__", "C", "fffffffffffffffe")   # 1 bit off → still in group
    image_hash.store(conn, "__mp__", "D", "0000000000000000")   # far → its own (excluded)
    conn.commit()
    groups = image_hash.duplicate_masterpiece_groups(conn)
    assert len(groups) == 1
    assert sorted(groups[0]) == ["A", "B", "C"]
    conn.close()


def test_not_duplicate_dismissal_persists():
    conn = get_connection()
    image_hash.ensure_table(conn)
    image_hash.store(conn, "__mp__", "A", "ffffffffffffffff")
    image_hash.store(conn, "__mp__", "B", "ffffffffffffffff")   # flagged as a look-alike of A
    conn.commit()
    assert image_hash.duplicate_masterpiece_groups(conn)        # grouped before dismissal
    # User says "not the same" → remembered, and the pair no longer groups.
    mq.add_not_duplicate(conn, ["A", "B"])
    dismissed = mq.not_duplicate_pairs(conn)
    assert dismissed == {("A", "B")}                            # normalised (a < b)
    assert image_hash.duplicate_masterpiece_groups(conn, dismissed=dismissed) == []
    conn.close()
