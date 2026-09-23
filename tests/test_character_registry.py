"""The character registry (spec 003, backlog CHARREG).

A character was a string typed into a comma box on each piece: no canonical spelling, no
dedup across works, no owner, no rename, and no route to a post — while the same
character already existed as a `name_(owner)` booru tag that reaches every post. This is
the People registry's shape minus handles, so the rules that matter are the same ones:
a key derived from the name, unsupplied fields left alone, and a rename that previews
before it rewrites the archive.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from database import artist_queries as aq
from database import character_queries as cq
from database.db import get_connection


@pytest.fixture()
def conn():
    c = get_connection()
    c.execute("DELETE FROM characters")
    c.commit()
    try:
        yield c
    finally:
        c.close()


@pytest.fixture()
def client(conn):
    from routes.characters_api import characters_router
    app = FastAPI()
    app.include_router(characters_router)
    return TestClient(app)


class TestOneCharacterIsOneRow:
    @pytest.mark.parametrize("spelling", ["Kii", "kii", "K I I", " kii "])
    def test_spellings_collapse_to_one_key(self, spelling):
        assert cq.character_key(spelling) == "kii"

    def test_punctuation_does_not_split_a_character(self):
        assert cq.character_key("Kii-the-Tiger") == cq.character_key("Kii The Tiger")

    def test_a_name_with_nothing_usable_is_refused(self, conn):
        with pytest.raises(ValueError):
            cq.upsert_character(conn, "   ")

    def test_a_typed_spelling_resolves_to_the_row(self, conn):
        cq.upsert_character(conn, "Sample Fox")
        conn.commit()
        assert cq.find_by_name(conn, "sample  FOX")["name"] == "Sample Fox"


class TestUpsertLeavesOtherFieldsAlone:
    """The picker sends a name; the registry page sends notes. Neither may wipe the
    other's work — the same KEEP sentinel the people registry uses."""

    def test_a_second_write_keeps_what_it_did_not_send(self, conn):
        cq.upsert_character(conn, "Sample Fox", booru_tag="sample_fox_(owner)", species="fox")
        cq.upsert_character(conn, "Sample Fox", notes="a note")
        conn.commit()
        row = cq.get_character(conn, "samplefox")
        assert row["booru_tag"] == "sample_fox_(owner)"
        assert row["species"] == "fox"
        assert row["notes"] == "a note"

    def test_an_explicit_empty_still_clears(self, conn):
        cq.upsert_character(conn, "Sample Fox", booru_tag="tag")
        cq.upsert_character(conn, "Sample Fox", booru_tag="")
        conn.commit()
        assert cq.get_character(conn, "samplefox")["booru_tag"] == ""


class TestBooruTags:
    def test_a_piece_contributes_its_characters_tags(self, conn):
        cq.upsert_character(conn, "Sample Fox", booru_tag="sample_fox_(owner)")
        cq.upsert_character(conn, "Second Fur", booru_tag="second_fur_(owner)")
        conn.commit()
        assert cq.booru_tags_for(conn, ["Sample Fox", "Second Fur"]) == \
            ["sample_fox_(owner)", "second_fur_(owner)"]

    def test_a_character_with_no_tag_adds_nothing(self, conn):
        """An invented tag is worse than a missing one — it is noise on every post."""
        cq.upsert_character(conn, "Untagged")
        conn.commit()
        assert cq.booru_tags_for(conn, ["Untagged"]) == []

    def test_an_unknown_name_adds_nothing(self, conn):
        assert cq.booru_tags_for(conn, ["Nobody At All"]) == []

    def test_the_same_tag_twice_appears_once(self, conn):
        cq.upsert_character(conn, "Sample Fox", booru_tag="shared_(owner)")
        cq.upsert_character(conn, "Alias Fox", booru_tag="shared_(owner)")
        conn.commit()
        assert cq.booru_tags_for(conn, ["Sample Fox", "Alias Fox"]) == ["shared_(owner)"]


class TestRename:
    def test_a_respelling_keeps_the_key_and_the_old_name_as_an_alias(self, conn):
        cq.upsert_character(conn, "sample fox")
        out = cq.rename_character(conn, "samplefox", "Sample Fox")
        conn.commit()
        assert out["rekeyed"] is False
        row = cq.get_character(conn, "samplefox")
        assert row["name"] == "Sample Fox" and "sample fox" in row["aliases"]

    def test_a_real_rename_moves_the_key_and_carries_the_record(self, conn):
        cq.upsert_character(conn, "Sample Fox", booru_tag="t", species="fox", owner_key="inkwolf")
        out = cq.rename_character(conn, "samplefox", "Sample Vixen")
        conn.commit()
        assert out["rekeyed"] is True and out["key"] == "samplevixen"
        assert cq.get_character(conn, "samplefox") is None
        moved = cq.get_character(conn, "samplevixen")
        assert (moved["booru_tag"], moved["species"], moved["owner_key"]) == ("t", "fox", "inkwolf")

    def test_renaming_onto_an_existing_character_is_refused(self, conn):
        cq.upsert_character(conn, "Sample Fox")
        cq.upsert_character(conn, "Sample Vixen")
        conn.commit()
        with pytest.raises(cq.CharacterExists):
            cq.rename_character(conn, "samplefox", "Sample Vixen")

    def test_the_route_previews_before_it_writes(self, client, conn):
        cq.upsert_character(conn, "Sample Fox")
        conn.commit()
        r = client.post("/api/characters/samplefox/rename", json={"new_name": "Sample Vixen"})
        assert r.json()["status"] == "preview"
        assert cq.get_character(conn, "samplefox") is not None, "a preview must write nothing"


class TestTheRoute:
    def test_an_owner_must_be_someone_in_people(self, client):
        r = client.post("/api/characters", json={"name": "Sample Fox", "owner_key": "ghost"})
        assert r.status_code == 400

    def test_a_real_owner_is_accepted_and_resolved_on_the_way_out(self, client, conn):
        aq.upsert_artist(conn, "Inkwolf")
        conn.commit()
        client.post("/api/characters", json={"name": "Sample Fox", "owner_key": "inkwolf"})
        row = next(c for c in client.get("/api/characters").json()["characters"]
                   if c["key"] == "samplefox")
        assert row["owner"]["name"] == "Inkwolf"

    def test_deleting_an_unused_character_just_works(self, client, conn):
        cq.upsert_character(conn, "Sample Fox")
        conn.commit()
        assert client.delete("/api/characters/samplefox").status_code == 200
        assert cq.get_character(conn, "samplefox") is None

    def test_a_name_is_required(self, client):
        assert client.post("/api/characters", json={"name": "  "}).status_code == 400
