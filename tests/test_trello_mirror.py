"""The board mirror's apply rules (spec 006, research R2/R5).

Every guarantee about not losing an edit lives in `trello/mirror.py` and is
driven here with plain dictionaries — no network. Fixture text is invented.
"""
from __future__ import annotations

import json

import pytest

from trello import mirror, outbox


def _read(cards=None, lists=None, labels=None, checklists=None, board="B1"):
    return {
        "id": board, "name": "Sample board", "closed": False, "url": "https://t/b",
        "prefs": {"backgroundColor": "#0079bf"},
        "lists": lists if lists is not None else [
            {"id": "L1", "name": "To do", "pos": 1, "closed": False},
            {"id": "L2", "name": "Done", "pos": 2, "closed": False}],
        "cards": cards if cards is not None else [],
        "labels": labels or [],
        "checklists": checklists or [],
    }


def _card(cid, **over):
    c = {"id": cid, "name": f"Sample {cid}", "desc": "", "pos": 1, "closed": False,
         "idList": "L1", "idLabels": [], "start": None, "due": None, "dueComplete": False,
         "cover": {}, "shortUrl": f"https://t/c/{cid}", "badges": {"comments": 0}}
    c.update(over)
    return c


@pytest.fixture
def conn(db_conn):
    return db_conn


def _cards(conn):
    return {r["id"]: mirror.row_dict("card", r)
            for r in conn.execute("SELECT * FROM trello_cards")}


class TestImport:

    def test_a_board_read_lands_lists_cards_labels_checklists(self, conn):
        read = _read(cards=[_card("C1", idLabels=["LB1"])],
                     labels=[{"id": "LB1", "name": "Urgent", "color": "red"}],
                     checklists=[{"id": "K1", "idCard": "C1", "name": "Steps", "pos": 1,
                                  "checkItems": [{"id": "I1", "name": "Sketch", "pos": 1,
                                                  "state": "complete"}]}])
        mirror.apply_board(conn, read)
        conn.commit()
        c = _cards(conn)["C1"]
        assert c["labels"] == ["LB1"] and c["list_id"] == "L1"
        assert c["agreed"]["name"] == "Sample C1", "a new row is agreed as read"
        assert mirror.get(conn, "checkitem", "I1")["state"] == "complete"
        b = mirror.get(conn, "board", "B1")
        assert b["bg_color"] == "#0079bf" and b["imported_at"]

    def test_archived_items_are_kept_as_archived_not_removed(self, conn):
        mirror.apply_board(conn, _read(cards=[_card("C1")]))
        mirror.apply_board(conn, _read(cards=[_card("C1", closed=True)]))
        c = _cards(conn)["C1"]
        assert c["closed"] == 1 and c["removed_at"] is None

    def test_a_card_missing_from_a_good_read_was_deleted_and_is_kept(self, conn):
        """FR-025: `cards=all` includes archived cards, so absence = deleted."""
        mirror.apply_board(conn, _read(cards=[_card("C1"), _card("C2")]))
        mirror.apply_board(conn, _read(cards=[_card("C1")]))
        c = _cards(conn)
        assert c["C2"]["removed_at"] and c["C2"]["name"] == "Sample C2"
        assert c["C1"]["removed_at"] is None

    def test_a_card_that_comes_back_is_restored(self, conn):
        mirror.apply_board(conn, _read(cards=[_card("C1"), _card("C2")]))
        mirror.apply_board(conn, _read(cards=[_card("C1")]))
        mirror.apply_board(conn, _read(cards=[_card("C1"), _card("C2")]))
        assert _cards(conn)["C2"]["removed_at"] is None

    def test_most_of_a_board_vanishing_stops_and_writes_nothing(self, conn):
        """FR-026: a wrong or emptied board looks like this; a person does not."""
        mirror.apply_board(conn, _read(cards=[_card(f"C{i}") for i in range(8)]))
        conn.commit()
        with pytest.raises(mirror.MassRemoval):
            mirror.apply_board(conn, _read(cards=[_card("C0")]))
        conn.rollback()
        assert not any(c["removed_at"] for c in _cards(conn).values())

    def test_the_guard_can_be_confirmed_through(self, conn):
        mirror.apply_board(conn, _read(cards=[_card(f"C{i}") for i in range(8)]))
        mirror.apply_board(conn, _read(cards=[_card("C0")]), confirm_removals=True)
        assert sum(1 for c in _cards(conn).values() if c["removed_at"]) == 7

    def test_a_card_moved_to_another_board_moves_rather_than_duplicates(self, conn):
        mirror.apply_board(conn, _read(cards=[_card("C1")]))
        mirror.apply_board(conn, _read(board="B2", lists=[
            {"id": "L9", "name": "Elsewhere", "pos": 1, "closed": False}],
            cards=[_card("C1", idList="L9")]))
        c = _cards(conn)["C1"]
        assert c["board_id"] == "B2" and c["removed_at"] is None
        mirror.apply_board(conn, _read(cards=[]))       # board 1 no longer has it
        assert _cards(conn)["C1"]["removed_at"] is None

    def test_an_empty_board_list_is_not_every_board_deleted(self, conn):
        mirror.apply_member_boards(conn, [{"id": "B1", "name": "One", "closed": False}])
        mirror.apply_member_boards(conn, [])
        assert mirror.get(conn, "board", "B1")["removed_at"] is None

    def test_a_deleted_board_is_kept(self, conn):
        mirror.apply_member_boards(conn, [{"id": "B1", "name": "One"}, {"id": "B2", "name": "Two"}])
        mirror.apply_member_boards(conn, [{"id": "B1", "name": "One"}])
        assert mirror.get(conn, "board", "B2")["removed_at"]


class TestThreeWay:

    def _edit(self, conn, cid, **fields):
        mirror.set_local(conn, "card", cid, fields)
        outbox.enqueue(conn, "card.update", "card", cid, fields)

    def test_a_remote_change_with_nothing_pending_is_taken(self, conn):
        mirror.apply_board(conn, _read(cards=[_card("C1")]))
        mirror.apply_board(conn, _read(cards=[_card("C1", desc="from the phone")]))
        assert _cards(conn)["C1"]["desc"] == "from the phone"

    def test_a_pending_local_edit_survives_an_unchanged_remote(self, conn):
        mirror.apply_board(conn, _read(cards=[_card("C1")]))
        self._edit(conn, "C1", desc="mine")
        mirror.apply_board(conn, _read(cards=[_card("C1")]))
        assert _cards(conn)["C1"]["desc"] == "mine"

    def test_the_same_field_changed_on_both_sides_is_a_conflict(self, conn):
        """FR-020: neither is written, both are kept, the op is held."""
        mirror.apply_board(conn, _read(cards=[_card("C1")]))
        self._edit(conn, "C1", desc="mine")
        stats = mirror.apply_board(conn, _read(cards=[_card("C1", desc="theirs")]))
        assert stats["card"]["conflicts"] == 1
        assert _cards(conn)["C1"]["desc"] == "mine"
        row = conn.execute("SELECT * FROM trello_mirror_conflicts").fetchone()
        assert json.loads(row["remote_value"]) == "theirs"
        assert json.loads(row["local_value"]) == "mine"
        assert conn.execute("SELECT state FROM trello_outbox").fetchone()["state"] == "held"

    def test_different_fields_on_each_side_both_apply(self, conn):
        mirror.apply_board(conn, _read(cards=[_card("C1")]))
        self._edit(conn, "C1", desc="mine")
        mirror.apply_board(conn, _read(cards=[_card("C1", idList="L2")]))
        c = _cards(conn)["C1"]
        assert c["desc"] == "mine" and c["list_id"] == "L2"
        assert not conn.execute("SELECT 1 FROM trello_mirror_conflicts").fetchone()

    def test_both_sides_agreeing_is_not_a_conflict(self, conn):
        mirror.apply_board(conn, _read(cards=[_card("C1")]))
        self._edit(conn, "C1", desc="same")
        mirror.apply_board(conn, _read(cards=[_card("C1", desc="same")]))
        assert not conn.execute("SELECT 1 FROM trello_mirror_conflicts").fetchone()
        assert _cards(conn)["C1"]["agreed"]["desc"] == "same"

    def test_position_never_conflicts_the_local_drag_wins(self, conn):
        mirror.apply_board(conn, _read(cards=[_card("C1", pos=1)]))
        mirror.set_local(conn, "card", "C1", {"pos": 5.0})
        outbox.enqueue(conn, "card.move", "card", "C1", {"list_id": "L1", "pos": 5.0})
        mirror.apply_board(conn, _read(cards=[_card("C1", pos=9)]))
        assert _cards(conn)["C1"]["pos"] == 5.0
        assert not conn.execute("SELECT 1 FROM trello_mirror_conflicts").fetchone()

    def test_labels_added_on_both_sides_merge(self, conn):
        mirror.apply_board(conn, _read(cards=[_card("C1", idLabels=["A"])]))
        mirror.set_local(conn, "card", "C1", {"labels": ["A", "MINE"]})
        outbox.enqueue(conn, "card.labels", "card", "C1", {"labels": ["A", "MINE"]})
        mirror.apply_board(conn, _read(cards=[_card("C1", idLabels=["A", "THEIRS"])]))
        assert _cards(conn)["C1"]["labels"] == ["A", "MINE", "THEIRS"]

    def test_a_failed_op_does_not_protect_the_local_value(self, conn):
        """Trello refused it, so Trello's value is the truth (FR-022)."""
        mirror.apply_board(conn, _read(cards=[_card("C1")]))
        self._edit(conn, "C1", desc="refused")
        conn.execute("UPDATE trello_outbox SET state = 'failed'")
        mirror.apply_board(conn, _read(cards=[_card("C1")]))
        assert _cards(conn)["C1"]["desc"] == ""

    def test_an_unsent_local_create_is_never_marked_removed(self, conn):
        mirror.apply_board(conn, _read(cards=[_card("C1")]))
        tmp = mirror.tmp_id()
        mirror.insert_local(conn, "card", tmp, {"board_id": "B1", "list_id": "L1", "name": "new"})
        mirror.apply_board(conn, _read(cards=[_card("C1")]))
        assert _cards(conn)[tmp]["removed_at"] is None


class TestRekey:

    def test_a_confirmed_create_is_rekeyed_everywhere(self, conn):
        mirror.apply_board(conn, _read())
        lid = mirror.tmp_id()
        mirror.insert_local(conn, "list", lid, {"board_id": "B1", "name": "New", "pos": 3})
        cid = mirror.tmp_id()
        mirror.insert_local(conn, "card", cid, {"board_id": "B1", "list_id": lid, "name": "x"})
        outbox.enqueue(conn, "card.move", "card", cid, {"list_id": lid, "pos": 1})
        mirror.rekey(conn, lid, "REAL_L")
        assert mirror.get(conn, "list", "REAL_L")
        assert _cards(conn)[cid]["list_id"] == "REAL_L"
        f = json.loads(conn.execute("SELECT fields FROM trello_outbox").fetchone()["fields"])
        assert f["list_id"] == "REAL_L"


class TestComments:

    def _action(self, aid, card, text):
        return {"id": aid, "date": "2026-01-01T00:00:00Z", "idMemberCreator": "M1",
                "memberCreator": {"fullName": "Sample Person"},
                "data": {"text": text, "card": {"id": card}}}

    def test_board_comments_are_added_and_never_removed_by_an_incremental_read(self, conn):
        mirror.apply_board(conn, _read(cards=[_card("C1")]), [self._action("A1", "C1", "hi")])
        mirror.apply_board(conn, _read(cards=[_card("C1")]), [self._action("A2", "C1", "again")])
        rows = {r["id"]: r for r in conn.execute("SELECT * FROM trello_comments")}
        assert set(rows) == {"A1", "A2"} and rows["A1"]["removed_at"] is None

    def test_a_cards_own_read_sees_edits_and_deletions(self, conn):
        mirror.apply_board(conn, _read(cards=[_card("C1")]),
                           [self._action("A1", "C1", "hi"), self._action("A2", "C1", "bye")])
        mirror.apply_card_comments(conn, "B1", "C1", [self._action("A1", "C1", "edited")])
        rows = {r["id"]: r for r in conn.execute("SELECT * FROM trello_comments")}
        assert rows["A1"]["text"] == "edited"
        assert rows["A2"]["removed_at"]
