"""One read pass of the mirror thread, end to end against a fake Trello.

⚠ Found on the first live run of 4.37.0: Trello names a personal Inbox board
(`members/me?fields=inbox`) but answers 401 when this token reads it. The pass
treated that as "the credentials are wrong" and aborted — so covers were never
fetched, every later pass failed identically, and Settings blamed the key.
A 401 on ONE board, after the member call succeeded, means that board only.
"""
from __future__ import annotations

import pytest

import config
from clients.trello.client import TrelloAuthError
from database.db import get_connection
from polling import trello_mirror
from trello import mapping, runtime


class Fake:
    def __init__(self):
        self.downloads = 0

    def me(self):
        return {"id": "M1", "username": "u", "full_name": "Sample"}

    def inbox_board_id(self):
        return "INBOX"

    def member_boards(self):
        return [{"id": "B1", "name": "Sample board", "prefs": {}}]

    def board_full(self, board_id):
        if board_id == "INBOX":
            raise TrelloAuthError("refused")
        return {"id": "B1", "name": "Sample board", "prefs": {},
                "lists": [{"id": "L1", "name": "l", "pos": 1}],
                "cards": [{"id": "C1", "name": "c", "idList": "L1", "pos": 1,
                           "cover": {"idAttachment": "a" * 24},
                           "attachments": [{"id": "a" * 24, "url": "https://trello.com/x.png",
                                            "fileName": "x.png", "mimeType": "image/png"}]}],
                "labels": [], "checklists": []}

    def board_comments(self, board_id, since=""):
        return []

    def card_comments(self, card_id):
        return []

    def download(self, url):
        self.downloads += 1
        return b"\x89PNG\r\n\x1a\n"

    def create_webhook(self, *a):
        raise AssertionError("webhooks are off")


@pytest.fixture
def fake(monkeypatch, tmp_path):
    f = Fake()
    monkeypatch.setattr(mapping, "client_from_settings", lambda settings=None: f)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    config.save_settings({"trello_api_key": "k" * 16, "trello_token": "t" * 16})
    runtime.set_error("")
    runtime.take_dirty()          # other tests leave boards/cards marked in this process
    return f


def test_an_unreadable_inbox_does_not_stop_the_pass(fake):
    trello_mirror.tick()
    conn = get_connection()
    try:
        assert conn.execute("SELECT imported_at FROM trello_boards WHERE id = 'B1'").fetchone()[0]
        assert conn.execute("SELECT removed_at FROM trello_boards WHERE id = 'INBOX'").fetchone()[0]
    finally:
        conn.close()
    assert fake.downloads == 1, "covers must still be fetched after the refused board"
    assert runtime.state["last_error"] == ""
    assert mapping.get_config()["inbox_id"] == "-"


def test_the_inbox_is_not_retried_every_pass(fake):
    trello_mirror.tick()
    calls = []
    orig = fake.board_full
    fake.board_full = lambda bid: (calls.append(bid), orig(bid))[1]
    trello_mirror.tick(force_all=True)
    assert "INBOX" not in calls
