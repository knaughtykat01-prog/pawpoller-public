"""Trello settings: the list → status map and which instance owns the connection.

Two things pinned here that are easy and expensive to get wrong:

1. **Lists are stored by id, never by name**, and an unmapped list means NO
   status — never a default (FR-031).
2. **One instance owns the connection.** Two instances each draining an outbox
   against their own mirror would fight over every field on every board.
"""
from __future__ import annotations

import config
from trello import mapping


def test_a_list_resolves_to_its_status():
    mapping.save_config(list_status={"L3": "wip"})
    assert mapping.status_for_list("L3") == "wip"


def test_an_unknown_list_means_nothing_rather_than_something():
    mapping.save_config(list_status={"L3": "wip"})
    assert mapping.status_for_list("L-nobody") is None
    assert mapping.status_for_list("") is None


def test_several_lists_can_share_a_status():
    mapping.save_config(list_status={"A": "wip", "B": "wip", "C": "paid"})
    assert sorted(mapping.lists_for_status("wip")) == ["A", "B"]


def test_an_invalid_status_is_dropped_not_stored():
    mapping.save_config(list_status={"A": "wip", "B": "not-a-status"})
    assert mapping.get_config()["list_status"] == {"A": "wip"}


def test_the_poll_interval_has_a_floor():
    mapping.save_config(poll_seconds=1)
    assert mapping.get_config()["poll_seconds"] == mapping.MIN_POLL


def test_webhook_mode_needs_the_switch_the_callback_and_the_secret():
    mapping.save_config(webhooks="auto", webhook_callback="https://x/hooks/trello")
    config.save_settings({"trello_secret": ""})
    assert mapping.mode() == "poll"
    config.save_settings({"trello_secret": "s" * 32})
    assert mapping.mode() == "webhook"


def test_the_first_instance_to_connect_owns_it():
    mapping.save_config(owner_tag="")
    assert mapping.owner_state() == (True, "")
    mapping.claim()
    assert mapping.owner_state()[0] is True


def test_another_instance_is_refused_by_name():
    mapping.save_config(owner_tag="someone-else")
    assert mapping.owner_state() == (False, "someone-else")


def test_the_instance_tag_never_syncs():
    assert mapping.TAG_KEY in config.SYNC_EXCLUDE


def test_the_secret_is_vaulted():
    import json
    assert "trello_secret" in config.CREDENTIAL_FIELDS
    config.save_settings({"trello_secret": "vault-me"})
    plain = json.loads(config.SETTINGS_PATH.read_text(encoding="utf-8"))
    assert "trello_secret" not in plain
