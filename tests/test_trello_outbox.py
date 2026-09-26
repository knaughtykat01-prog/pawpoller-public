"""The outbox: ordered, offline-safe sending (spec 006, research R4).

A fake client stands in for Trello. What is pinned: order, temp-id re-keying,
``agreed`` advancing only after Trello accepts, the three failure paths, and the
client-side refusal to delete a board, list or card.
"""
from __future__ import annotations

import json

import pytest

from clients.trello.client import (TrelloAuthError, TrelloClient, TrelloError,
                                   TrelloRetryableError)
from trello import mirror, outbox, runtime


class Fake:
    def __init__(self):
        self.calls = []
        self.fail = None
        self.n = 0

    def _maybe_fail(self):
        if self.fail:
            e, self.fail = self.fail, None
            raise e

    def create_card(self, *, id_list, name, pos="bottom", **_):
        self._maybe_fail()
        self.n += 1
        self.calls.append(("create_card", id_list, name))
        return {"id": f"REAL{self.n}", "pos": 99.0, "shortUrl": "https://t/c"}

    def create_list(self, board_id, name, pos="bottom"):
        self._maybe_fail()
        self.n += 1
        self.calls.append(("create_list", board_id, name))
        return {"id": f"LIST{self.n}", "pos": 7.0}

    def update_card(self, card_id, **fields):
        self._maybe_fail()
        self.calls.append(("update_card", card_id, fields))
        return {"pos": fields.get("pos")}

    def add_label(self, card_id, label_id):
        self.calls.append(("add_label", card_id, label_id))

    def remove_label(self, card_id, label_id):
        self.calls.append(("remove_label", card_id, label_id))


@pytest.fixture
def conn(db_conn):
    db_conn.execute("INSERT INTO trello_boards (id, name) VALUES ('B1', 'b')")
    db_conn.execute("INSERT INTO trello_lists (id, board_id, name, pos, agreed) "
                    "VALUES ('L1', 'B1', 'l', 1, '{}')")
    db_conn.execute("INSERT INTO trello_cards (id, board_id, list_id, name, agreed) "
                    "VALUES ('C1', 'B1', 'L1', 'Sample', ?)",
                    (json.dumps({"name": "Sample", "desc": "", "labels": []}),))
    db_conn.commit()
    runtime._outbox_paused_until = 0.0
    return db_conn


def _drain(conn, client):
    out = []
    while True:
        r = outbox.drain_one(conn, client)
        out.append(r)
        if r in ("empty", "wait", "auth"):
            return out


def test_ops_are_sent_in_the_order_they_were_made(conn):
    f = Fake()
    for d in ("one", "two", "three"):
        mirror.set_local(conn, "card", "C1", {"desc": d})
        outbox.enqueue(conn, "card.update", "card", "C1", {"desc": d})
    conn.commit()
    _drain(conn, f)
    assert [c[2]["desc"] for c in f.calls] == ["one", "two", "three"]
    assert mirror.get(conn, "card", "C1")["agreed"]["desc"] == "three"


def test_agreed_does_not_advance_until_trello_accepts(conn):
    f = Fake()
    f.fail = TrelloRetryableError("offline")
    mirror.set_local(conn, "card", "C1", {"desc": "mine"})
    outbox.enqueue(conn, "card.update", "card", "C1", {"desc": "mine"})
    conn.commit()
    assert outbox.drain_one(conn, f) == "wait"
    assert mirror.get(conn, "card", "C1")["agreed"]["desc"] == ""
    assert outbox.counts(conn)["pending"] == 1, "the op survives being offline"


def test_a_create_offline_then_a_move_is_rekeyed_and_sent_in_order(conn):
    f = Fake()
    lid = mirror.tmp_id()
    mirror.insert_local(conn, "list", lid, {"board_id": "B1", "name": "New", "pos": 2})
    outbox.enqueue(conn, "list.create", "list", lid, {"name": "New", "pos": 2})
    mirror.set_local(conn, "card", "C1", {"list_id": lid, "pos": 1})
    outbox.enqueue(conn, "card.move", "card", "C1", {"list_id": lid, "pos": 1})
    conn.commit()
    _drain(conn, f)
    assert f.calls[0][0] == "create_list"
    assert f.calls[1] == ("update_card", "C1", {"idList": "LIST1", "pos": 1})
    assert mirror.get(conn, "card", "C1")["list_id"] == "LIST1"
    assert mirror.get(conn, "list", "LIST1") and not mirror.get(conn, "list", lid)


def test_a_refused_create_fails_visibly_and_hides_the_orphan(conn):
    f = Fake()
    f.fail = TrelloError("Trello refused the request (400).")
    cid = mirror.tmp_id()
    mirror.insert_local(conn, "card", cid, {"board_id": "B1", "list_id": "L1", "name": "x"})
    outbox.enqueue(conn, "card.create", "card", cid, {"name": "x", "pos": 1, "list_id": "L1"})
    conn.commit()
    assert outbox.drain_one(conn, f) == "failed"
    assert outbox.failed(conn)[0]["error"].startswith("Trello refused")
    assert mirror.get(conn, "card", cid)["removed_at"]


def test_auth_failure_stops_sending_and_keeps_the_op(conn):
    f = Fake()
    f.fail = TrelloAuthError("no")
    mirror.set_local(conn, "card", "C1", {"desc": "x"})
    outbox.enqueue(conn, "card.update", "card", "C1", {"desc": "x"})
    conn.commit()
    assert outbox.drain_one(conn, f) == "auth"
    assert outbox.counts(conn)["pending"] == 1


def test_label_changes_are_sent_as_deltas_from_agreed(conn):
    f = Fake()
    conn.execute("UPDATE trello_cards SET agreed = ? WHERE id = 'C1'",
                 (json.dumps({"labels": ["A", "B"]}),))
    mirror.set_local(conn, "card", "C1", {"labels": ["B", "C"]})
    outbox.enqueue(conn, "card.labels", "card", "C1", {"labels": ["B", "C"]})
    conn.commit()
    _drain(conn, f)
    assert ("add_label", "C1", "C") in f.calls and ("remove_label", "C1", "A") in f.calls
    assert ("add_label", "C1", "B") not in f.calls


def test_an_op_behind_a_held_op_on_the_same_card_waits(conn):
    f = Fake()
    outbox.enqueue(conn, "card.update", "card", "C1", {"desc": "held"})
    conn.execute("UPDATE trello_outbox SET state = 'held'")
    outbox.enqueue(conn, "card.update", "card", "C1", {"name": "later"})
    conn.commit()
    assert outbox.drain_one(conn, f) == "empty"
    assert f.calls == []


class TestTheClientCannotDelete:
    """⚠ FR-024: boards, lists and cards archive. The guard is in the one place
    every request passes, so no future method can bypass it."""

    @pytest.mark.parametrize("path", ["/cards/abc", "/boards/abc", "/lists/abc",
                                      "/cards/abc/attachments/x"])
    def test_delete_of_a_board_list_or_card_is_refused_before_the_network(self, path):
        c = TrelloClient("k" * 16, "t" * 16)
        with pytest.raises(ValueError):
            c._request("DELETE", path)

    def test_the_deletable_things_are_the_ones_trello_cannot_archive(self):
        from clients.trello.client import _DELETABLE
        for ok in ("/labels/x", "/checklists/x", "/actions/x", "/webhooks/x",
                   "/cards/c/idLabels/l", "/checklists/k/checkItems/i"):
            assert _DELETABLE.match(ok), ok

    @pytest.mark.parametrize("url", ["https://evil.example/x.png", "http://trello.com/x",
                                     "https://trello.com.evil.example/x",
                                     "https://evil.example/?https://trello.com/"])
    def test_credentials_are_never_sent_to_a_host_that_is_not_trello(self, url):
        """A link attachment's URL can point anywhere; the download header
        carries the key and token."""
        with pytest.raises(TrelloError):
            TrelloClient("k" * 16, "t" * 16).download(url)

    def test_an_upstream_body_is_never_quoted(self):
        """A 4xx on a card write echoes the card, whose title can be a client."""
        src = open("clients/trello/client.py", encoding="utf-8").read()
        i = src.index("if r.status_code >= 400:")
        assert "r.text" not in src[i:i + 400]
