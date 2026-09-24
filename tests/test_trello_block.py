"""The card description block (spec 005, research R3).

Trello cards have no price field, so price, currency and the correlation key ride
in a delimited block at the end of the description — the field the operator also
edits.

⚠ That overlap is the whole reason this file exists. `strip()` has to remove the
block from both sides before any description comparison; miss it and every sync
sees the description as changed and raises a conflict against itself, on every
card, for ever. It is the loudest wrong behaviour the feature can produce and the
cheapest to prevent.
"""
from __future__ import annotations

from trello import card_block


class TestRoundTrip:

    def test_what_is_rendered_can_be_read_back(self):
        block = card_block.render(key="k1", price=120.5, currency="AUD",
                                  url="http://example.test/#/commissions/1")
        got = card_block.parse(block)
        assert got["key"] == "k1"
        assert got["price"] == 120.5
        assert got["currency"] == "AUD"
        assert got["url"] == "http://example.test/#/commissions/1"

    def test_a_block_survives_being_appended_to_real_text(self):
        desc = card_block.apply("Two characters, full colour.",
                                card_block.render(key="k1", price=80, currency="USD"))
        assert card_block.parse(desc)["price"] == 80.0
        assert card_block.strip(desc) == "Two characters, full colour."

    def test_price_is_optional(self):
        got = card_block.parse(card_block.render(key="k1"))
        assert got["key"] == "k1" and "price" not in got


class TestStripping:
    """The load-bearing half."""

    def test_the_operators_text_comes_back_unchanged(self):
        original = "Line one.\n\nLine two, with a - dash and a : colon."
        desc = card_block.apply(original, card_block.render(key="k", price=1))
        assert card_block.strip(desc) == original

    def test_a_description_with_no_block_is_untouched(self):
        assert card_block.strip("just text") == "just text"

    def test_an_empty_description_is_not_an_error(self):
        assert card_block.strip("") == ""
        assert card_block.parse("") == {}

    def test_a_block_only_description_strips_to_nothing(self):
        assert card_block.strip(card_block.render(key="k")) == ""

    def test_two_blocks_are_both_removed(self):
        """A copied card carries a duplicate. One is noise, not data."""
        desc = ("text\n\n" + card_block.render(key="a") + "\n\n"
                + card_block.render(key="b"))
        assert card_block.strip(desc) == "text"

    def test_stripping_is_idempotent(self):
        """The sync strips on every read; twice must equal once."""
        desc = card_block.apply("text", card_block.render(key="k"))
        once = card_block.strip(desc)
        assert card_block.strip(once) == once


class TestItNeverRaises:
    """A card whose block the operator edited by hand is a card we rewrite on the
    next push — not a sync that falls over."""

    def test_a_mangled_block_parses_to_nothing(self):
        assert card_block.parse("<!-- pawpoller:do-not-edit\nnonsense") == {}

    def test_an_unreadable_price_is_absent_rather_than_zero(self):
        """Zero is a number somebody might have meant."""
        desc = "<!-- pawpoller:do-not-edit\nkey:   k\nprice: not-a-number\n-->"
        got = card_block.parse(desc)
        assert got["key"] == "k"
        assert "price" not in got

    def test_text_mentioning_the_marker_is_not_a_block(self):
        assert card_block.parse("I typed pawpoller:do-not-edit into my notes") == {}

    def test_a_block_missing_its_close_is_not_half_read(self):
        assert card_block.parse("text\n<!-- pawpoller:do-not-edit\nkey: k") == {}


class TestTheKey:

    def test_it_is_the_commissions_natural_key(self):
        """The same pair the mirror matches on — see research R1. A card carrying
        it is how the link table can be rebuilt if it is ever lost."""
        assert card_block.make_key("Sample Client", "2026-01-01 00:00:00") == \
            "Sample Client|2026-01-01 00:00:00"

    def test_the_key_round_trips_through_a_card(self):
        key = card_block.make_key("Sample Client", "2026-01-01 00:00:00")
        desc = card_block.apply("", card_block.render(key=key))
        assert card_block.parse(desc)["key"] == key
