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


# ── Confidence (4.32.3): the duplicates page works down from the sure things ──

def test_group_confidence_is_the_weakest_pair():
    """A group is chained from near-identical pairs, so its ends can be further apart
    than any single edge. Quoting the closest pair would oversell it."""
    conn = get_connection()
    image_hash.ensure_table(conn)
    image_hash.store(conn, "__mp__", "A", "ffffffffffffffff")
    image_hash.store(conn, "__mp__", "B", "ffffffffffffffff")   # identical to A
    image_hash.store(conn, "__mp__", "C", "fffffffffffffff0")   # 4 bits off A
    conn.commit()
    assert image_hash.group_confidence(conn, ["A", "B"]) == 1.0
    assert image_hash.group_confidence(conn, ["A", "B", "C"]) == 1.0 - 4 / 64
    conn.close()


def test_group_confidence_needs_two_hashes():
    conn = get_connection()
    image_hash.ensure_table(conn)
    image_hash.store(conn, "__mp__", "Lonely", "ffffffffffffffff")
    conn.commit()
    assert image_hash.group_confidence(conn, ["Lonely"]) == 0.0
    assert image_hash.group_confidence(conn, []) == 0.0
    conn.close()


def test_the_api_lists_certain_groups_first_and_labels_them():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routes.masterpieces_api import masterpieces_router

    conn = get_connection()
    image_hash.ensure_table(conn)
    # One pair that is byte-identical, one pair that is merely close.
    image_hash.store(conn, "__mp__", "Sure A", "ffffffffffffffff")
    image_hash.store(conn, "__mp__", "Sure B", "ffffffffffffffff")
    image_hash.store(conn, "__mp__", "Maybe A", "00000000000000ff")
    image_hash.store(conn, "__mp__", "Maybe B", "00000000000000f0")
    conn.commit()
    conn.close()

    app = FastAPI()
    app.include_router(masterpieces_router)      # the router carries its own prefix
    groups = TestClient(app).get("/api/masterpieces/duplicates").json()["groups"]
    confs = [g[0]["confidence"] for g in groups]
    assert confs == sorted(confs, reverse=True), confs
    assert all(0.0 <= c <= 1.0 for c in confs)
