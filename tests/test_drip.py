"""Drip scheduling (gap G1) — the posting_queue surface.

The endpoint mirrors schedule_publish's battle-tested validation; what's NEW at
the data layer is the drip_group column: rows created together share it,
cancel_all_for can cancel exactly one campaign, and get_scheduled_items
surfaces it for the Queue page's "Cancel whole drip" action.
"""
from database.db import get_connection
from database import posting_queries


def _enqueue_drip(conn, group, chapters, platform="ib"):
    ids = []
    for i, ch in enumerate(chapters):
        ids.append(posting_queries.add_to_queue(
            conn, "Drip_Story", ch, platform,
            scheduled_at=f"2030-01-{i + 1:02d} 20:00:00",
            drip_group=group,
            title_override=f"💧 drip {i + 1}/{len(chapters)}",
        ))
    return ids


def test_drip_rows_share_group_and_are_scheduled():
    conn = get_connection()
    try:
        _enqueue_drip(conn, "grp_aaa", [1, 2, 3])
        items = posting_queries.get_scheduled_items(conn)
    finally:
        conn.close()
    assert len(items) == 3
    assert {i["drip_group"] for i in items} == {"grp_aaa"}
    # Soonest first, staggered one day apart.
    assert [i["scheduled_at"] for i in items] == [
        "2030-01-01 20:00:00", "2030-01-02 20:00:00", "2030-01-03 20:00:00"]
    assert items[0]["title_override"].startswith("💧 drip 1/")


def test_cancel_drip_cancels_only_that_group():
    conn = get_connection()
    try:
        _enqueue_drip(conn, "grp_one", [1, 2])
        _enqueue_drip(conn, "grp_two", [1, 2, 3])
        # A plain one-off scheduled item with no group.
        posting_queries.add_to_queue(conn, "Other_Story", 0, "sf",
                                     scheduled_at="2030-02-01 09:00:00")

        n = posting_queries.cancel_all_for(conn, drip_group="grp_one")
        assert n == 2

        remaining = posting_queries.get_scheduled_items(conn)
    finally:
        conn.close()
    # grp_two (3) + the one-off survive; grp_one is gone.
    assert len(remaining) == 4
    assert all(i["drip_group"] != "grp_one" for i in remaining)
    assert sum(1 for i in remaining if i["drip_group"] == "grp_two") == 3


def test_ordinary_items_have_null_drip_group():
    conn = get_connection()
    try:
        posting_queries.add_to_queue(conn, "Solo", 1, "ib",
                                     scheduled_at="2030-03-01 12:00:00")
        items = posting_queries.get_scheduled_items(conn)
    finally:
        conn.close()
    assert items[0]["drip_group"] is None
