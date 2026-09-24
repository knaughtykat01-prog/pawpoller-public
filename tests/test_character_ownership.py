""""This character is mine" — a character owned by one of the operator's personas.

4.34.1 answered this by labelling any People row that carried a persona link. The
code was correct and the feature was unusable: the People registry exists to credit
OTHER artists, so claiming your own character meant first creating a row about
yourself inside it, then linking that row to a persona.

Measured on the live database before this change: **46 People rows, none
persona-linked**, so the "you ·" group was always empty and nobody ever saw it. One
character of three had any owner at all.

So a character now carries `persona_id` directly, and the operator's personas are
first-class options in the same dropdown. No setup.
"""
from __future__ import annotations

import sqlite3

import pytest

from database import character_queries as cq


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("""CREATE TABLE characters (
        character_key TEXT PRIMARY KEY, name TEXT NOT NULL, owner_key TEXT DEFAULT '',
        persona_id INTEGER, booru_tag TEXT DEFAULT '', species TEXT DEFAULT '',
        notes TEXT DEFAULT '', aliases TEXT DEFAULT '[]',
        created_at TEXT DEFAULT (datetime('now')),
        updated_at TEXT DEFAULT (datetime('now')))""")
    return c


class TestClaimingACharacter:

    def test_a_new_character_is_nobodys_by_default(self, conn):
        row = cq.upsert_character(conn, "Sample Character")
        assert row["persona_id"] is None and row["owner_key"] == ""

    def test_it_can_be_claimed_for_a_persona(self, conn):
        row = cq.upsert_character(conn, "Sample Character", persona_id=1)
        assert row["persona_id"] == 1

    def test_a_select_sends_strings(self, conn):
        """The <option> value arrives as text; the column is an integer."""
        row = cq.upsert_character(conn, "Sample Character", persona_id="4")
        assert row["persona_id"] == 4

    def test_the_blank_option_clears_it(self, conn):
        cq.upsert_character(conn, "Sample Character", persona_id=1)
        for blank in ("", None, 0, "0"):
            row = cq.upsert_character(conn, "Sample Character", persona_id=blank)
            assert row["persona_id"] is None, f"{blank!r} should mean 'not mine'"
            cq.upsert_character(conn, "Sample Character", persona_id=1)

    def test_rubbish_does_not_become_an_owner(self, conn):
        row = cq.upsert_character(conn, "Sample Character", persona_id="not-a-number")
        assert row["persona_id"] is None


class TestTheTwoAnswersAreMutuallyExclusive:
    """`owner_key` (a People row) and `persona_id` (one of yours) answer the same
    question. A character holding both would make the badge and the booru tag
    disagree about whose it is — and the tag travels to e621 and Furbooru, so the
    disagreement ends up on live posts."""

    def test_claiming_it_drops_the_previous_owner(self, conn):
        cq.upsert_character(conn, "Sample Character", owner_key="someone")
        row = cq.upsert_character(conn, "Sample Character", persona_id=1)
        assert row["persona_id"] == 1
        assert row["owner_key"] == "", "it cannot be theirs AND mine"

    def test_giving_it_away_drops_the_persona(self, conn):
        cq.upsert_character(conn, "Sample Character", persona_id=1)
        row = cq.upsert_character(conn, "Sample Character", owner_key="someone")
        assert row["owner_key"] == "someone"
        assert row["persona_id"] is None

    def test_clearing_the_owner_does_not_silently_claim_it(self, conn):
        """Blanking the People row means "owner unknown", not "therefore mine"."""
        cq.upsert_character(conn, "Sample Character", owner_key="someone")
        row = cq.upsert_character(conn, "Sample Character", owner_key="")
        assert row["owner_key"] == "" and row["persona_id"] is None

    def test_an_unrelated_edit_keeps_the_claim(self, conn):
        """The registry page sends notes and a species; it must not wipe ownership."""
        cq.upsert_character(conn, "Sample Character", persona_id=1)
        row = cq.upsert_character(conn, "Sample Character", species="fox", notes="hi")
        assert row["persona_id"] == 1, "an unsupplied field is left alone"


class TestTheRouteRefusesAPersonaThatIsNotYours:

    def test_it_validates_against_the_real_list(self):
        """An arbitrary integer would render a blank badge rather than a wrong one,
        which is harder to notice than a refusal."""
        src = open("routes/characters_api.py", encoding="utf-8").read()
        i = src.index("def upsert_character") if "def upsert_character" in src else 0
        assert "not one of your personas" in src
        assert "personas_db.list_personas" in src

    def test_the_list_route_resolves_the_persona_name(self):
        src = open("routes/characters_api.py", encoding="utf-8").read()
        assert '"persona"' in src, "the page needs a name to badge, not just an id"
        assert '"personas"' in src, "and the dropdown needs the options"


class TestTheUiNeedsNoSetup:
    """The whole point: the previous design required creating a row about yourself
    before you could say a character was yours."""

    def test_personas_are_options_in_their_own_right(self):
        src = open("frontend/js/characters.js", encoding="utf-8").read()
        assert "persona:${p.persona_id}" in src
        assert "mine · " in src

    def test_it_no_longer_depends_on_a_persona_linked_people_row(self):
        src = open("frontend/js/characters.js", encoding="utf-8").read()
        assert "this._people.filter(p => p.persona_id != null)" not in src, \
            "that filter was empty on real data for every install"

    def test_saving_always_sends_both_keys(self):
        """Sending only the chosen one leaves the other in place, because the backend
        treats an absent field as 'leave alone' — so a character moved from a friend
        to yourself would claim both."""
        src = open("frontend/js/characters.js", encoding="utf-8").read()
        i = src.index("data-ch-owner=")
        block = src[src.index("const osel"):]
        assert "body.persona_id = null" in block
        assert "body.owner_key = ''" in block

    def test_there_is_a_way_to_see_only_yours(self):
        src = open("frontend/js/characters.js", encoding="utf-8").read()
        assert "'mine', 'Mine'" in src
        assert "counts = { all: 0, mine: 0" in src, "an uninitialised count renders NaN"
