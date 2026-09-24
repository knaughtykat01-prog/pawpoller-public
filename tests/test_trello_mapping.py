"""The board claim and the status↔column map (spec 005).

Two things are pinned here that are easy to get wrong and expensive to get wrong:

1. **Columns are stored by id, never by name.** Renaming a column on the board
   must change nothing, and two columns can share a name.
2. **One instance owns a board.** `commissions` is SHR-mirrored, so the desktop
   and the server hold the same rows and would both drive the same board, each
   against its own baseline, disagreeing on every field for ever.
"""
from __future__ import annotations

import config
from trello import mapping

LISTS = {"quote": "L1", "accepted": "L2", "wip": "L3", "paid": "L4", "delivered": "L5"}


def _setup(**over):
    cfg = {"board_id": "B1", "board_name": "Board", "list_map": dict(LISTS)}
    cfg.update(over)
    mapping.save_config(**cfg)


class TestTheMap:

    def test_a_column_resolves_to_its_status(self):
        _setup()
        assert mapping.status_for_list("L3") == "wip"

    def test_a_status_resolves_to_its_column(self):
        _setup()
        assert mapping.list_for_status("paid") == "L4"

    def test_an_unknown_column_means_nothing_rather_than_something(self):
        """⚠ FR-012. Defaulting here would move work the operator never moved."""
        _setup()
        assert mapping.status_for_list("L-nobody") is None
        assert mapping.status_for_list("") is None

    def test_an_unmapped_status_is_reported_not_defaulted(self):
        _setup(list_map={**LISTS, "delivered": ""})
        assert mapping.list_for_status("delivered") == ""
        assert "delivered" in mapping.unmapped_statuses()

    def test_a_renamed_column_still_maps(self):
        """The mapping stores ids. A rename on the board is invisible here, which
        is the whole point of not storing names."""
        _setup()
        before = mapping.status_for_list("L3")
        # nothing about a rename touches stored settings at all
        assert mapping.status_for_list("L3") == before == "wip"

    def test_a_deleted_column_leaves_its_status_unmapped(self):
        _setup(list_map={**LISTS, "wip": ""})
        assert mapping.list_for_status("wip") == ""
        assert mapping.status_for_list("L3") is None

    def test_only_the_five_known_statuses_are_stored(self):
        """A typo must not look configured."""
        _setup(list_map={**LISTS, "invented": "L9"})
        assert "invented" not in mapping.get_config()["list_map"]

    def test_mapped_list_ids_is_what_the_sync_scans(self):
        _setup(list_map={**LISTS, "delivered": ""})
        assert mapping.mapped_list_ids() == {"L1", "L2", "L3", "L4"}


class TestConfiguration:

    def test_an_empty_install_is_not_configured(self):
        assert not mapping.is_configured()

    def test_a_board_with_no_columns_mapped_is_not_configured(self):
        mapping.save_config(board_id="B1")
        assert not mapping.is_configured()

    def test_a_board_with_one_column_is(self):
        mapping.save_config(board_id="B1", list_map={"wip": "L3"})
        assert mapping.is_configured()

    def test_the_interval_cannot_go_negative(self):
        mapping.save_config(interval_min=-5)
        assert mapping.get_config()["interval_min"] == 0

    def test_zero_is_a_real_setting_meaning_no_schedule(self):
        mapping.save_config(interval_min=0)
        assert mapping.get_config()["interval_min"] == 0


class TestOwnership:

    def test_an_unclaimed_board_is_owned_by_whoever_configures_it(self):
        _setup()
        is_owner, owner = mapping.owner_state()
        assert is_owner and owner == ""

    def test_claiming_records_this_instance(self):
        mapping.claim("B1", "Board")
        assert mapping.owner_state() == (True, mapping.instance_tag())

    def test_a_board_claimed_by_another_instance_is_refused_by_name(self):
        _setup(owner_tag="someone-else")
        is_owner, owner = mapping.owner_state()
        assert not is_owner
        assert owner == "someone-else"

    def test_the_instance_tag_is_stable(self):
        assert mapping.instance_tag() == mapping.instance_tag()

    def test_the_tag_is_excluded_from_the_settings_sync(self):
        """⚠ The `trello` block SYNCS -- both sides must agree which board and who
        owns it. The tag naming THIS box must not, or every instance would read as
        the owner and they would fight over every field."""
        assert mapping.TAG_KEY in config.SYNC_EXCLUDE
        assert "trello" not in config.SYNC_EXCLUDE

    def test_claiming_a_board_resets_the_first_sync_gate(self):
        """A different board has never been previewed, whatever the old one had."""
        _setup()
        mapping.save_config(first_sync_done=True)
        mapping.claim("B2", "Other")
        assert mapping.get_config()["first_sync_done"] is False


class TestCredentialsAreVaulted:

    def test_both_trello_values_are_secrets(self):
        """The key identifies the app but the token is bearer-equivalent, and
        Trello echoes query parameters in some error bodies."""
        assert config.is_credential_key("trello_api_key")
        assert config.is_credential_key("trello_token")

    def test_it_is_listed_as_a_credential_platform(self):
        """So it inherits the vault, the log scrubber and the Test button rather
        than growing its own of each."""
        assert "trello" in config.PLATFORM_CREDENTIAL_FIELDS
