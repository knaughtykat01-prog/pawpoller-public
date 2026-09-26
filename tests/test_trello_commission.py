"""Commissions on the board (spec 006, Q1 → option A; research R10).

Pinned: a card mark creates a commission and unmarking KEEPS it; status, due
date and archived follow the card; a change on the Commissions page moves the
card through the outbox; the 005 → 006 migration previews before it writes.
Client names here are invented.
"""
from __future__ import annotations

import json

import pytest

from database import commissions_queries as cq
from database import trello_queries as tq
from trello import card_block, commission, mapping, mirror


@pytest.fixture
def conn(db_conn):
    db_conn.execute("INSERT INTO trello_boards (id, name) VALUES ('B1', 'b')")
    for lid, pos in (("LQ", 1), ("LW", 2), ("LP", 3)):
        db_conn.execute("INSERT INTO trello_lists (id, board_id, name, pos) VALUES (?, 'B1', ?, ?)",
                        (lid, lid, pos))
    db_conn.execute("INSERT INTO trello_cards (id, board_id, list_id, name, pos, due) "
                    "VALUES ('C1', 'B1', 'LW', 'Inkwolf ref sheet', 1, '2026-10-01T12:00:00.000Z')")
    db_conn.commit()
    mapping.save_config(commission_board_id="B1",
                        list_status={"LQ": "quote", "LW": "wip", "LP": "paid"})
    return db_conn


def test_marking_a_card_makes_a_commission_from_it(conn):
    c = commission.mark(conn, "C1", price=120, currency="AUD")
    assert c["client_name"] == "Inkwolf ref sheet"
    assert c["status"] == "wip" and c["due_date"] == "2026-10-01"
    assert commission.card_commission(conn, "C1")["id"] == c["id"]


def test_a_card_cannot_be_marked_twice(conn):
    commission.mark(conn, "C1")
    with pytest.raises(ValueError):
        commission.mark(conn, "C1")


def test_unmarking_keeps_the_commission(conn):
    c = commission.mark(conn, "C1")
    assert commission.unmark(conn, "C1")
    assert cq.get_commission(conn, c["id"]) is not None
    assert commission.card_commission(conn, "C1") is None


def test_status_due_and_archive_follow_the_card(conn):
    c = commission.mark(conn, "C1")
    conn.execute("UPDATE trello_cards SET list_id = 'LP', due = NULL, closed = 1 WHERE id = 'C1'")
    commission.sync_from_board(conn, "B1")
    after = cq.get_commission(conn, c["id"])
    assert after["status"] == "paid" and after["due_date"] == "" and after["archived"] == 1


def test_an_unmapped_list_keeps_the_last_status(conn):
    """FR-031: a list that means nothing never moves the commission."""
    c = commission.mark(conn, "C1")
    conn.execute("INSERT INTO trello_lists (id, board_id, name, pos) VALUES ('LX', 'B1', 'x', 9)")
    conn.execute("UPDATE trello_cards SET list_id = 'LX' WHERE id = 'C1'")
    commission.sync_from_board(conn, "B1")
    assert cq.get_commission(conn, c["id"])["status"] == "wip"


def test_a_card_deleted_in_trello_unlinks_and_keeps_the_commission(conn):
    c = commission.mark(conn, "C1")
    conn.execute("UPDATE trello_cards SET removed_at = '2026-01-01' WHERE id = 'C1'")
    commission.sync_from_board(conn, "B1")
    assert cq.get_commission(conn, c["id"]) is not None
    assert tq.get_link_by_card(conn, "C1") is None


def test_a_status_change_on_the_commissions_page_moves_the_card(conn):
    c = commission.mark(conn, "C1")
    assert commission.push_change(conn, c, status="paid")
    assert mirror.get(conn, "card", "C1")["list_id"] == "LP"
    op = conn.execute("SELECT * FROM trello_outbox").fetchone()
    assert op["op"] == "card.move" and json.loads(op["fields"])["list_id"] == "LP"


def test_the_route_carries_status_and_archive_to_the_card(conn):
    """And fixes the Archive button, which sent `archived` to a route that
    dropped it."""
    from routes import commissions_api
    c = commission.mark(conn, "C1")
    commissions_api.update_commission(c["id"], {"archived": 1})
    assert cq.get_commission(conn, c["id"])["archived"] == 1
    ops = [r["op"] for r in conn.execute("SELECT op FROM trello_outbox")]
    assert "card.update" in ops


class TestMigrationFrom005:

    def _legacy(self, conn):
        mapping.save_config(commission_board_id="", list_status={}, board_id="B1",
                            list_map={"quote": "LQ", "wip": "LW", "paid": "LP"})
        cid = cq.create_commission(conn, client_name="Penwright", status="wip")
        conn.commit()
        c = cq.get_commission(conn, cid)
        conn.execute("UPDATE trello_cards SET desc = ? WHERE id = 'C1'",
                     ("Notes\n\n" + card_block.render(key="k", price=5),))
        tq.create_link(conn, client_name=c["client_name"], created_at=c["created_at"],
                       card_id="C1", board_id="B1")
        cq.create_commission(conn, client_name="SecondFur", status="quote")
        conn.commit()

    def test_the_preview_writes_nothing(self, conn):
        self._legacy(conn)
        plan = commission.migration_plan(conn)
        assert plan["list_status"] == {"LQ": "quote", "LW": "wip", "LP": "paid"}
        assert [s["card_id"] for s in plan["strip"]] == ["C1"]
        assert [c["client_name"] for c in plan["create"]] == ["SecondFur"]
        assert conn.execute("SELECT COUNT(*) FROM trello_outbox").fetchone()[0] == 0

    def test_applying_strips_the_block_and_gives_cardless_commissions_a_card(self, conn):
        self._legacy(conn)
        r = commission.migrate(conn)
        assert r == {"ok": True, "stripped": 1, "created": 1}
        assert mirror.get(conn, "card", "C1")["desc"] == "Notes"
        assert mapping.get_config()["commission_board_id"] == "B1"
        ops = [r["op"] for r in conn.execute("SELECT op FROM trello_outbox ORDER BY seq")]
        assert ops == ["card.update", "card.create"]
        new = [l for l in tq.list_links(conn) if l["client_name"] == "SecondFur"][0]
        assert mirror.is_tmp(new["card_id"])
        assert mirror.get(conn, "card", new["card_id"])["list_id"] == "LQ"
