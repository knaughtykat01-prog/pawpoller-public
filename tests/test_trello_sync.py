"""The sync, end to end, against a fake board (spec 005).

No network. `FakeTrello` holds cards in a dict and records what was asked of it,
which is enough to pin every behaviour that matters — and, deliberately, enough to
fail the run at an arbitrary point so the interrupted-sync guarantee can be tested
rather than asserted.

The three properties this file exists for:

1. A baseline advances only after the write it describes succeeded (FR-021).
2. A failed read unlinks nothing (research R7).
3. Nothing deletes anything, on either side.
"""
from __future__ import annotations

import pytest

import config
from clients.trello.client import TrelloAuthError, TrelloError
from database import commissions_queries as cq
from database import trello_queries as tq
from database.db import get_connection, init_db
from trello import card_block, mapping, sync

LISTS = {"quote": "L1", "accepted": "L2", "wip": "L3", "paid": "L4", "delivered": "L5"}


class FakeTrello:
    """A board in a dict. Counts calls so a test can assert what was NOT done."""

    def __init__(self, cards=None, fail_read=None, fail_write_after=None):
        self.store = {c["id"]: dict(c) for c in (cards or [])}
        self.fail_read = fail_read              # an exception to raise on cards()
        self.fail_write_after = fail_write_after  # succeed N writes, then raise
        self.writes = 0
        self.created = []
        self.updated = []
        self.deleted = []                       # must stay empty, always

    def cards(self, board_id):
        if self.fail_read:
            raise self.fail_read
        return [dict(c) for c in self.store.values() if not c.get("closed")]

    def card(self, card_id):
        c = self.store.get(card_id)
        return dict(c) if c else None

    def _writing(self):
        if self.fail_write_after is not None and self.writes >= self.fail_write_after:
            raise TrelloError("fake write failure")
        self.writes += 1

    def create_card(self, *, id_list, name, desc="", due=""):
        self._writing()
        cid = f"C{len(self.store) + 1}"
        card = {"id": cid, "name": name, "desc": desc, "due": due, "closed": False,
                "id_list": id_list, "last_activity": "", "url": f"http://t/{cid}"}
        self.store[cid] = card
        self.created.append(card)
        return dict(card)

    def update_card(self, card_id, **fields):
        self._writing()
        card = self.store[card_id]
        for k, v in fields.items():
            card[{"idList": "id_list"}.get(k, k)] = v
        self.updated.append((card_id, fields))
        return dict(card)

    def archive_card(self, card_id):
        return self.update_card(card_id, closed=True)


@pytest.fixture
def conn():
    init_db()
    c = get_connection()
    yield c
    c.close()


@pytest.fixture
def board(monkeypatch):
    mapping.save_config(board_id="B1", list_map=dict(LISTS), first_sync_done=True)
    config.save_settings({"trello_api_key": "k" * 12, "trello_token": "t" * 12})
    fake = FakeTrello()
    monkeypatch.setattr(sync, "client_from_settings", lambda settings=None: fake)
    return fake


def _commission(conn, **over):
    fields = {"client_name": "Sample Client", "description": "a piece",
              "price": 100.0, "currency": "AUD", "status": "wip",
              "due_date": "2026-03-01"}
    fields.update(over)
    cid = cq.create_commission(conn, **fields)   # returns the rowid, not the row
    conn.commit()
    return cq.get_commission(conn, cid)


def _key(c):
    return c["client_name"], c["created_at"]


# ── Push ─────────────────────────────────────────────────────────────────────

class TestPush:

    def test_a_commission_becomes_a_card_in_the_right_column(self, conn, board):
        c = _commission(conn)
        r = sync.run_sync(conn, apply=True)
        assert r["counts"]["created"] == 1
        assert board.created[0]["id_list"] == "L3"
        assert board.created[0]["name"].startswith("Sample Client")

    def test_the_card_carries_the_price_in_its_block(self, conn, board):
        _commission(conn)
        sync.run_sync(conn, apply=True)
        assert card_block.parse(board.created[0]["desc"])["price"] == 100.0

    def test_a_status_change_moves_the_card_and_nothing_else(self, conn, board):
        c = _commission(conn)
        sync.run_sync(conn, apply=True)
        board.updated.clear()
        cq.update_commission(conn, c["id"], status="paid")
        conn.commit()
        sync.run_sync(conn, apply=True)
        assert len(board.updated) == 1
        _, fields = board.updated[0]
        assert fields["idList"] == "L4"
        assert "name" not in fields and "desc" not in fields, \
            "a status-only move must not rewrite the card's text (FR-007)"

    def test_an_archived_commission_archives_its_card(self, conn, board):
        c = _commission(conn)
        sync.run_sync(conn, apply=True)
        cq.set_archived(conn, c["id"], True)
        conn.commit()
        sync.run_sync(conn, apply=True)
        assert board.store[board.created[0]["id"]]["closed"] is True
        assert board.deleted == [], "cards are archived, never deleted"

    def test_an_archived_commission_gets_no_new_card(self, conn, board):
        c = _commission(conn)
        cq.set_archived(conn, c["id"], True)
        conn.commit()
        r = sync.run_sync(conn, apply=True)
        assert r["counts"]["created"] == 0

    def test_an_unmapped_status_is_skipped_and_named(self, conn, board):
        mapping.save_config(list_map={**LISTS, "wip": ""})
        _commission(conn, status="wip")
        r = sync.run_sync(conn, apply=True)
        assert r["counts"]["created"] == 0
        assert r["skipped"][0]["reason"] == "status_unmapped"

    def test_a_second_sync_with_nothing_changed_does_nothing(self, conn, board):
        _commission(conn)
        sync.run_sync(conn, apply=True)
        board.updated.clear()
        r = sync.run_sync(conn, apply=True)
        assert board.updated == []
        assert all(v == 0 for k, v in r["counts"].items() if k != "skipped")


# ── Pull ─────────────────────────────────────────────────────────────────────

class TestPull:

    def test_a_card_dragged_to_another_column_changes_the_status(self, conn, board):
        c = _commission(conn)
        sync.run_sync(conn, apply=True)
        board.store[board.created[0]["id"]]["id_list"] = "L4"
        sync.run_sync(conn, apply=True)
        assert cq.get_commission(conn, c["id"])["status"] == "paid"

    def test_a_description_edited_in_trello_comes_back(self, conn, board):
        c = _commission(conn)
        sync.run_sync(conn, apply=True)
        card = board.store[board.created[0]["id"]]
        block = card_block.render(key=card_block.make_key(*_key(c)), price=100,
                                  currency="AUD")
        card["desc"] = card_block.apply("their words", block)
        sync.run_sync(conn, apply=True)
        assert cq.get_commission(conn, c["id"])["description"] == "their words"

    def test_a_price_edited_in_the_block_comes_back(self, conn, board):
        c = _commission(conn)
        sync.run_sync(conn, apply=True)
        card = board.store[board.created[0]["id"]]
        card["desc"] = card_block.apply(
            "a piece", card_block.render(key=card_block.make_key(*_key(c)),
                                         price=250, currency="AUD"))
        sync.run_sync(conn, apply=True)
        assert cq.get_commission(conn, c["id"])["price"] == 250.0

    def test_a_card_in_an_unmapped_column_leaves_the_status_alone(self, conn, board):
        c = _commission(conn)
        sync.run_sync(conn, apply=True)
        board.store[board.created[0]["id"]]["id_list"] = "L-unmapped"
        r = sync.run_sync(conn, apply=True)
        assert cq.get_commission(conn, c["id"])["status"] == "wip"
        assert any(s["reason"] == "card_in_unmapped_column" for s in r["skipped"])

    def test_a_hand_made_card_is_a_candidate_and_creates_nothing(self, conn, board):
        board.store["X1"] = {"id": "X1", "name": "Someone else's card", "desc": "",
                             "due": "", "closed": False, "id_list": "L3",
                             "last_activity": "", "url": "http://t/X1"}
        r = sync.run_sync(conn, apply=True)
        assert r["counts"]["candidates"] == 1
        assert cq.list_commissions(conn) == []

    def test_a_card_outside_every_mapped_column_is_not_even_noise(self, conn, board):
        board.store["X1"] = {"id": "X1", "name": "Other work", "desc": "", "due": "",
                             "closed": False, "id_list": "L-other",
                             "last_activity": "", "url": "http://t/X1"}
        assert sync.run_sync(conn, apply=True)["counts"]["candidates"] == 0


# ── Conflicts ────────────────────────────────────────────────────────────────

class TestConflicts:

    def _diverge(self, conn, board):
        c = _commission(conn)
        sync.run_sync(conn, apply=True)
        card = board.store[board.created[0]["id"]]
        cq.update_commission(conn, c["id"], price=150.0)
        conn.commit()
        card["desc"] = card_block.apply(
            "a piece", card_block.render(key=card_block.make_key(*_key(c)),
                                         price=200, currency="AUD"))
        return c, card

    def test_both_sides_changed_writes_neither(self, conn, board):
        c, card = self._diverge(conn, board)
        board.updated.clear()
        r = sync.run_sync(conn, apply=True)
        assert r["counts"]["conflicted"] == 1
        assert cq.get_commission(conn, c["id"])["price"] == 150.0, "ours is untouched"
        assert card_block.parse(card["desc"])["price"] == 200.0, "theirs is untouched"
        assert board.updated == [], "nothing was written to the board"

    def test_the_conflict_records_both_values_and_their_origin(self, conn, board):
        c, _ = self._diverge(conn, board)
        sync.run_sync(conn, apply=True)
        row = tq.list_conflicts(conn)[0]
        assert row["field"] == "price"
        assert row["local_value"] == "150.0" and row["remote_value"] == "200.0"

    def test_a_conflict_does_not_stop_other_commissions(self, conn, board):
        self._diverge(conn, board)
        _commission(conn, client_name="Second Client")
        r = sync.run_sync(conn, apply=True)
        assert r["counts"]["conflicted"] == 1
        assert r["counts"]["created"] == 1

    def test_a_conflicted_field_stays_blocked_until_resolved(self, conn, board):
        c, _ = self._diverge(conn, board)
        sync.run_sync(conn, apply=True)
        board.updated.clear()
        sync.run_sync(conn, apply=True)
        assert board.updated == []
        assert cq.get_commission(conn, c["id"])["price"] == 150.0


# ── Deletion never cascades ──────────────────────────────────────────────────

class TestDeletionNeverCascades:

    def test_a_deleted_card_keeps_the_commission_and_unlinks_it(self, conn, board):
        c = _commission(conn)
        sync.run_sync(conn, apply=True)
        board.store.clear()
        r = sync.run_sync(conn, apply=True)
        assert r["counts"]["unlinked"] == 1
        kept = cq.get_commission(conn, c["id"])
        assert kept["price"] == 100.0 and kept["description"] == "a piece"
        assert tq.get_link(conn, *_key(c)) is None

    def test_an_auth_failure_unlinks_nothing(self, conn, board):
        """⚠ Research R7 — the cheapest way to make every commission look deleted
        is for the credentials to be revoked."""
        c = _commission(conn)
        sync.run_sync(conn, apply=True)
        board.fail_read = TrelloAuthError("nope")
        r = sync.run_sync(conn, apply=True)
        assert r["counts"]["unlinked"] == 0
        assert tq.get_link(conn, *_key(c)) is not None
        assert r["errors"]

    def test_a_mass_unlink_stops_and_asks(self, conn, board):
        for i in range(4):
            _commission(conn, client_name=f"Client {i}")
        sync.run_sync(conn, apply=True)
        board.store.clear()
        r = sync.run_sync(conn, apply=True)
        assert r["counts"]["unlinked"] == 0
        assert r["errors"]

    def test_confirming_lets_the_mass_unlink_through(self, conn, board):
        for i in range(4):
            _commission(conn, client_name=f"Client {i}")
        sync.run_sync(conn, apply=True)
        board.store.clear()
        r = sync.run_sync(conn, apply=True, confirm_unlinks=True)
        assert r["counts"]["unlinked"] == 4
        assert len(cq.list_commissions(conn)) == 4, "every commission survives"


# ── Safety ───────────────────────────────────────────────────────────────────

class TestPreviewAndOwnership:

    def test_preview_writes_nothing(self, conn, board):
        _commission(conn)
        r = sync.run_sync(conn, apply=False)
        assert r["counts"]["created"] == 1 and r["applied"] is False
        assert board.created == []

    def test_the_first_sync_previews_even_when_told_to_apply(self, conn, board):
        mapping.save_config(first_sync_done=False)
        _commission(conn)
        r = sync.run_sync(conn, apply=True)
        assert r["applied"] is False
        assert board.created == []
        assert r["errors"]

    def test_a_board_owned_by_another_instance_is_refused(self, conn, board):
        mapping.save_config(owner_tag="another-box")
        _commission(conn)
        r = sync.run_sync(conn, apply=True)
        assert board.created == []
        assert "another-box" in r["errors"][0]

    def test_an_unconfigured_install_does_nothing_and_says_so(self, conn, monkeypatch):
        config.save_settings({"trello": {}})
        r = sync.run_sync(conn, apply=True)
        assert r["errors"]


class TestAnInterruptedSyncLosesNothing:
    """FR-021. The baseline advances per field, after the write that field needed."""

    def test_a_failed_write_leaves_the_change_pending(self, conn, board):
        c = _commission(conn)
        sync.run_sync(conn, apply=True)
        cq.update_commission(conn, c["id"], price=175.0)
        conn.commit()
        board.fail_write_after = board.writes          # next write fails
        sync.run_sync(conn, apply=True)
        base = tq.get_link(conn, *_key(c))["baseline"]
        assert base["price"] == 100.0, "an unwritten field must not look agreed"

        board.fail_write_after = None
        sync.run_sync(conn, apply=True)
        assert tq.get_link(conn, *_key(c))["baseline"]["price"] == 175.0

    def test_a_link_is_never_stored_for_a_card_that_was_not_created(self, conn, board):
        _commission(conn)
        board.fail_write_after = 0
        sync.run_sync(conn, apply=True)
        assert tq.list_links(conn) == []


class TestTheRepairPath:
    """The card carries the commission's natural key, so a lost link table is
    recoverable rather than a board full of duplicates."""

    def test_a_card_naming_us_is_relinked_rather_than_duplicated(self, conn, board):
        c = _commission(conn)
        sync.run_sync(conn, apply=True)
        tq.delete_link(conn, *_key(c))
        board.created.clear()
        r = sync.run_sync(conn, apply=True)
        assert r["counts"]["created"] == 0, "it must not make a second card"
        assert tq.get_link(conn, *_key(c)) is not None
