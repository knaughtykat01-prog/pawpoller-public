"""People, not just artists (4.6.0) — docs/specs/people_registry.md.

The registry was a people registry with one role hard-coded. This adds a
persona link (a person row can BE one of the operator's personas), a per-handle
mention switch, ``people[]`` with roles on a piece, and the featuring line —
and fixes the thing ``own`` got wrong: a self-drawn piece posted to a booru
carried no artist tag, on the one site where the artist tag IS the index.

Three rules, each with its own test:
  * "is the artist me" is a fact about the piece AND the publish (which persona
    is posting), never a global mode;
  * a person is LINKED on a site only where that handle's mention flag is on —
    a link notifies, names are free, links are consent;
  * the piece stores the registry row's key and the package RE-READS the row, so
    a corrected handle reaches every piece — but a deleted row never un-credits.
"""
from __future__ import annotations

import json
import os
import tempfile

import pytest

from database import artist_queries as aq
from posting import artist_credit as ac
from posting import artwork_reader as ar


# ── fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture()
def conn(monkeypatch):
    import config
    from database import db as dbm
    monkeypatch.setattr(config, "DB_PATH", os.path.join(tempfile.mkdtemp(), "pp.db"))
    dbm.init_db()
    c = dbm.get_connection()
    yield c
    c.close()


def _persona_with_account(conn, name, platform="e621"):
    from database import accounts, personas
    pid = personas.create_persona(conn, name)
    aid = accounts.create_account(conn, platform, f"{name} {platform}")
    personas.assign_account_persona(conn, aid, pid)
    conn.commit()
    return pid, aid


@pytest.fixture()
def archive(tmp_path, monkeypatch):
    root = tmp_path / "artwork"
    root.mkdir()
    monkeypatch.setattr(ar, "get_artwork_archive_path", lambda: root)
    return root


def _piece(root, name, **meta):
    folder = root / name
    folder.mkdir()
    (folder / "image.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    data = {"title": name.replace("_", " "), "description": "A blurb.", "rating": "general",
            "image": "image.png", "tags": {"core": ["anthro"]}}
    data.update(meta)
    (folder / "masterpiece.json").write_text(json.dumps(data), encoding="utf-8")
    return name


INK = {"name": "Inkwolf", "handles": {"fa": "inkwolf", "e621": "ink_wolf"}}
SECOND = {"name": "SecondFur", "handles": {"fa": "secondfur", "ib": "SecondFur", "bsky": "second.fur.example"},
          "mention": {"fa": True, "ib": True, "bsky": True}}
THIRD = {"name": "ThirdFur", "handles": {"fa": "thirdfur"}, "mention": {}}


# ── the registry ─────────────────────────────────────────────────────────────

class TestRegistry:
    def test_the_two_columns_exist_after_migration(self, conn):
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(artists)")}
        assert "persona_id" in cols
        hcols = {r["name"] for r in conn.execute("PRAGMA table_info(artist_handles)")}
        assert "mention" in hcols

    def test_persona_link_round_trips_and_is_kept_when_not_supplied(self, conn):
        key = aq.upsert_artist(conn, "Inkwolf", handles={"fa": "inkwolf"}, persona_id=2)
        assert aq.get_artist(conn, key)["persona_id"] == 2
        aq.upsert_artist(conn, "Inkwolf", handles={"tw": "inkwolf"})       # no persona_id kwarg
        assert aq.get_artist(conn, key)["persona_id"] == 2, "an unsupplied persona must not clear the link"
        aq.upsert_artist(conn, "Inkwolf", persona_id=None)
        assert aq.get_artist(conn, key)["persona_id"] is None

    def test_mention_is_per_handle_default_off_and_survives_a_handle_rewrite(self, conn):
        key = aq.upsert_artist(conn, "SecondFur", handles={"fa": "secondfur", "tw": "secondfur"})
        a = aq.get_artist(conn, key)
        assert a["mention"] == {}, "off everywhere until switched on"
        aq.upsert_artist(conn, "SecondFur", mention={"fa": True})
        a = aq.get_artist(conn, key)
        assert a["mention"] == {"fa": True}
        # Correcting the handle keeps the consent that was given for that site.
        aq.upsert_artist(conn, "SecondFur", handles={"fa": "second_fur"})
        a = aq.get_artist(conn, key)
        assert a["handles"]["fa"] == "second_fur" and a["mention"] == {"fa": True}
        aq.upsert_artist(conn, "SecondFur", mention={"fa": False})
        assert aq.get_artist(conn, key)["mention"] == {}

    def test_person_for_persona_and_people_for(self, conn):
        k1 = aq.upsert_artist(conn, "Inkwolf", persona_id=1)
        k2 = aq.upsert_artist(conn, "SecondFur")
        assert aq.person_for_persona(conn, 1)["key"] == k1
        assert aq.person_for_persona(conn, 9) is None
        assert aq.person_for_persona(conn, None) is None
        rows = aq.people_for(conn, [k1, k2, "nobody"])
        assert set(rows) == {k1, k2}, "unknown keys are simply absent"

    def test_find_by_handle_is_case_insensitive(self, conn):
        k = aq.upsert_artist(conn, "SecondFur", handles={"fa": "SecondFur"})
        assert aq.find_by_handle(conn, "fa", "secondfur") == [k]
        assert aq.find_by_handle(conn, "ib", "secondfur") == []


# ── the credit when the artist is you ────────────────────────────────────────

class TestSelf:
    @pytest.mark.parametrize("platform", ["fa", "ib", "ws", "e621", "fn", "ik", "bsky", "da", "sf", "tw", "tg"])
    def test_no_credit_line_on_any_site(self, platform):
        assert ac.render(INK, platform, is_self=True) == ""
        assert ac.append_to("blurb", INK, platform, is_self=True) == "blurb"

    def test_the_booru_tag_is_the_persons_e621_handle_then_their_name(self):
        assert ac.artist_tag(INK, prefer_handle=True) == "ink_wolf"
        assert ac.artist_tag({"name": "Ink Wolf", "handles": {"fa": "inkwolf"}}, prefer_handle=True) == "ink_wolf"
        assert ac.artist_tag(INK) == "inkwolf", "the default is unchanged: the name"


# ── the featuring line ───────────────────────────────────────────────────────

def _p(role, person, character=""):
    return {"role": role, "person": person, "character": character}


class TestFeaturing:
    def test_linked_only_where_that_handle_may_mention(self):
        assert ac.featuring([_p("commissioner", SECOND)], "fa") == "for :iconsecondfur:"
        assert ac.featuring([_p("commissioner", THIRD)], "fa") == "for ThirdFur"
        # SecondFur has a tw handle? No — and mention on FA does not leak to X.
        assert ac.featuring([_p("commissioner", SECOND)], "tw") == "for SecondFur"

    def test_each_site_uses_its_own_markup_through_render_link(self):
        assert ac.featuring([_p("collaborator", SECOND)], "ib") == "with [name]SecondFur[/name]"
        assert ac.featuring([_p("collaborator", SECOND)], "bsky") == "with @second.fur.example"

    def test_an_owner_reads_featuring_their_character_grouped_per_person(self):
        people = [_p("owner", SECOND, "Alpha"), _p("owner", SECOND, "Beta"), _p("owner", THIRD, "Gamma")]
        assert ac.featuring(people, "fa") == "featuring :iconsecondfur:'s Alpha and Beta and ThirdFur's Gamma"

    def test_fixed_order_and_unknown_roles_dropped(self):
        people = [_p("collaborator", THIRD), _p("owner", SECOND, "Alpha"), _p("commissioner", SECOND),
                  _p("artist", THIRD), {"role": "owner", "person": {"name": ""}}]
        assert ac.featuring(people, "tw") == "for SecondFur · featuring SecondFur's Alpha · with ThirdFur"

    def test_append_people_is_idempotent_and_sits_under_the_credit(self):
        people = [_p("commissioner", SECOND)]
        once = ac.append_people("blurb", people, "fa")
        assert once == "blurb\n\nfor :iconsecondfur:"
        assert ac.append_people(once, people, "fa") == once
        # Already named by hand at a word boundary → nothing added.
        assert ac.append_people("Drawn for SecondFur.", people, "fa") == "Drawn for SecondFur."
        # Right after the credit line: one newline, one block.
        assert ac.append_people("blurb\n\nArt by :iconinkwolf:", people, "fa", after_credit=True) \
            == "blurb\n\nArt by :iconinkwolf:\nfor :iconsecondfur:"
        assert ac.append_people("", people, "fa") == "for :iconsecondfur:"

    def test_the_roles_are_one_tuple_in_both_modules(self):
        assert ac.ROLES == ar.PEOPLE_ROLES == ("commissioner", "owner", "collaborator")


# ── what the piece stores ────────────────────────────────────────────────────

class TestReader:
    def test_the_artist_blob_keeps_its_key_only_when_it_has_one(self):
        assert ar._clean_artist({"name": "Inkwolf", "handles": {"fa": "inkwolf"}}) \
            == {"name": "Inkwolf", "handles": {"fa": "inkwolf"}}
        assert ar._clean_artist({"key": "inkwolf", "name": "Inkwolf", "handles": {}}) \
            == {"key": "inkwolf", "name": "Inkwolf", "handles": {}}

    def test_people_are_validated_not_trusted(self):
        raw = [{"key": "secondfur", "role": "commissioner"},
               {"key": "thirdfur", "role": "owner", "character": "Alpha"},
               {"key": "", "role": "owner", "character": "x"},
               {"key": "x", "role": "artist"}, "junk", {"key": "y", "role": "OWNER", "character": " B "}]
        assert ar._clean_people(raw) == [
            {"key": "secondfur", "role": "commissioner"},
            {"key": "thirdfur", "role": "owner", "character": "Alpha"},
            {"key": "y", "role": "owner", "character": "B"},
        ]
        assert ar._clean_people(None) == [] and ar._clean_people("nope") == []

    def test_load_and_list_carry_people(self, archive):
        _piece(archive, "With_People", people=[{"key": "secondfur", "role": "collaborator"}])
        assert ar.load_artwork("With_People").people == [{"key": "secondfur", "role": "collaborator"}]
        assert ar.list_artworks()[0]["people"] == [{"key": "secondfur", "role": "collaborator"}]


# ── the package ──────────────────────────────────────────────────────────────

class TestPackage:
    def test_a_self_drawn_piece_gets_the_tag_and_no_credit_line(self, conn, archive):
        ink, e6 = _persona_with_account(conn, "Inkwolf")
        key = aq.upsert_artist(conn, "Inkwolf", handles=INK["handles"], persona_id=ink)
        conn.commit()
        _piece(archive, "Mine", artist={"key": key, **INK})
        pkg = ar.build_artwork_package(ar.load_artwork("Mine"), "e621", account_id=e6)
        assert pkg.tags[0] == "ink_wolf", "the booru artist tag is the person's e621 handle"
        assert "Art by" not in pkg.description

    def test_the_same_piece_posted_by_another_persona_is_credited(self, conn, archive):
        ink, _ = _persona_with_account(conn, "Inkwolf")
        pen, pen_e6 = _persona_with_account(conn, "Penwright")
        key = aq.upsert_artist(conn, "Inkwolf", handles=INK["handles"], persona_id=ink)
        conn.commit()
        _piece(archive, "Theirs", artist={"key": key, **INK})
        pkg = ar.build_artwork_package(ar.load_artwork("Theirs"), "e621", account_id=pen_e6)
        assert "Art by" in pkg.description and pkg.tags[0] == "inkwolf"

    def test_own_with_no_row_still_tags_when_the_persona_has_a_person(self, conn, archive):
        ink, e6 = _persona_with_account(conn, "Inkwolf")
        aq.upsert_artist(conn, "Inkwolf", handles=INK["handles"], persona_id=ink)
        conn.commit()
        _piece(archive, "Own_Legacy", artist_status="own")
        pkg = ar.build_artwork_package(ar.load_artwork("Own_Legacy"), "e621", account_id=e6)
        assert pkg.tags[0] == "ink_wolf" and "Art by" not in pkg.description

    def test_own_without_an_account_is_unchanged(self, archive):
        _piece(archive, "Own_Plain", artist_status="own")
        pkg = ar.build_artwork_package(ar.load_artwork("Own_Plain"), "e621")
        assert "Art by" not in pkg.description and pkg.tags == ["anthro"]

    def test_the_registry_row_is_re_read_by_key_and_a_deleted_row_falls_back(self, conn, archive):
        key = aq.upsert_artist(conn, "Inkwolf", handles={"fa": "old_handle"})
        conn.commit()
        _piece(archive, "Keyed", artist={"key": key, "name": "Inkwolf", "handles": {"fa": "old_handle"}})
        aq.upsert_artist(conn, "Inkwolf", handles={"fa": "corrected"})       # fixed on the People page
        conn.commit()
        pkg = ar.build_artwork_package(ar.load_artwork("Keyed"), "fa")
        assert "Art by :iconcorrected:" in pkg.description
        conn.execute("DELETE FROM artists WHERE artist_key = ?", (key,))
        conn.execute("DELETE FROM artist_handles WHERE artist_key = ?", (key,))
        conn.commit()
        pkg = ar.build_artwork_package(ar.load_artwork("Keyed"), "fa")
        assert "Art by :iconoldhandle:" in pkg.description, "a deleted person must not un-credit the piece"

    def test_people_become_the_featuring_line_and_unknown_keys_are_skipped(self, conn, archive):
        k = aq.upsert_artist(conn, "SecondFur", handles=SECOND["handles"], mention={"fa": True})
        conn.commit()
        _piece(archive, "Commissioned", artist=INK, characters=["Alpha"],
               people=[{"key": k, "role": "commissioner"}, {"key": k, "role": "owner", "character": "Alpha"},
                       {"key": "ghost", "role": "collaborator"}])
        # (`in`, not `endswith`: the "Posted via PawPoller" line follows by default.)
        pkg = ar.build_artwork_package(ar.load_artwork("Commissioned"), "fa")
        assert "Art by :iconinkwolf:\nfor :iconsecondfur: · featuring :iconsecondfur:'s Alpha" in pkg.description
        assert "ghost" not in pkg.description
        pkg = ar.build_artwork_package(ar.load_artwork("Commissioned"), "tw")
        assert "\nfor SecondFur · featuring SecondFur's Alpha" in pkg.description


# ── the one-shot key link ────────────────────────────────────────────────────

class TestLinkKeys:
    def test_a_unique_match_gets_its_key_and_an_ambiguous_one_is_left_alone(self, conn, archive):
        k1 = aq.upsert_artist(conn, "Inkwolf", handles={"fa": "inkwolf"})
        aq.upsert_artist(conn, "SecondFur", handles={"tw": "shared"})
        aq.upsert_artist(conn, "ThirdFur", handles={"bsky": "shared"})
        conn.commit()
        _piece(archive, "By_Name", artist={"name": "Ink Wolf", "handles": {}})
        _piece(archive, "By_Handle", artist={"name": "Somebody", "handles": {"fa": "INKWOLF"}})
        _piece(archive, "Ambiguous", artist={"name": "Nobody", "handles": {"tw": "shared", "bsky": "shared"}})
        _piece(archive, "Already", artist={"key": "kept", "name": "Inkwolf", "handles": {}})
        _piece(archive, "Unknown", artist={"name": "Fresh Face", "handles": {}})
        out = ar.link_artist_keys()
        assert sorted(out["linked"]) == ["By_Handle", "By_Name"]
        assert out["ambiguous"] == ["Ambiguous"]
        assert ar.load_artwork("By_Name").artist["key"] == k1
        assert ar.load_artwork("By_Handle").artist["key"] == k1
        assert "key" not in ar.load_artwork("Ambiguous").artist
        assert ar.load_artwork("Already").artist["key"] == "kept"
        assert "key" not in ar.load_artwork("Unknown").artist, "never invents a key"
        assert ar.link_artist_keys() == {"linked": [], "ambiguous": ["Ambiguous"]}, "idempotent"


# ── the API ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def client(conn):
    from fastapi.testclient import TestClient
    from dashboard import app
    return TestClient(app)


class TestApi:
    def test_people_are_validated_on_the_way_in(self, client, conn, archive):
        k = aq.upsert_artist(conn, "SecondFur")
        conn.commit()
        _piece(archive, "P", characters=["Alpha"])
        r = client.patch("/api/masterpieces/P", json={"people": [{"key": "ghost", "role": "commissioner"}]})
        assert r.status_code == 400 and "ghost" in r.text
        r = client.patch("/api/masterpieces/P", json={"people": [{"key": k, "role": "owner"}]})
        assert r.status_code == 400 and "character" in r.text
        r = client.patch("/api/masterpieces/P", json={"people": [{"key": k, "role": "boss"}]})
        assert r.status_code == 400
        r = client.patch("/api/masterpieces/P", json={"people": [{"key": k, "role": "owner", "character": "Alpha"},
                                                                  {"key": k, "role": "commissioner"}]})
        assert r.status_code == 200, r.text
        d = client.get("/api/masterpieces/P").json()
        assert [p["role"] for p in d["people"]] == ["owner", "commissioner"]
        assert d["people"][0]["name"] == "SecondFur" and d["people"][0]["character"] == "Alpha"

    def test_setting_an_artist_stores_the_registry_key(self, client, conn, archive):
        _piece(archive, "K")
        r = client.patch("/api/masterpieces/K", json={"artist": {"name": "Inkwolf", "handles": {"fa": "inkwolf"}}})
        assert r.status_code == 200, r.text
        assert ar.load_artwork("K").artist["key"] == aq.artist_key("Inkwolf")

    def test_my_own_work_with_a_persona_upgrades_to_the_linked_row(self, client, conn, archive):
        ink, _ = _persona_with_account(conn, "Inkwolf")
        key = aq.upsert_artist(conn, "Inkwolf", handles=INK["handles"], persona_id=ink)
        conn.commit()
        _piece(archive, "Own1")
        r = client.patch("/api/masterpieces/Own1", json={"artist": None, "artist_status": "own", "own_persona_id": ink})
        assert r.status_code == 200, r.text
        a = ar.load_artwork("Own1")
        assert a.artist["key"] == key and a.artist_status == ""
        d = client.get("/api/masterpieces/Own1").json()
        assert d["artist_persona_id"] == ink
        # No row for that persona → the no-row form of the same claim.
        _piece(archive, "Own2")
        r = client.patch("/api/masterpieces/Own2", json={"artist": None, "artist_status": "own", "own_persona_id": 77})
        assert r.status_code == 200 and ar.load_artwork("Own2").artist_status == "own"

    def test_the_registry_api_carries_persona_and_mention(self, client, conn):
        r = client.post("/api/artists", json={"name": "SecondFur", "handles": {"fa": "secondfur"},
                                              "mention": {"fa": True}, "persona_id": None})
        assert r.status_code == 200, r.text
        assert r.json()["mention"] == {"fa": True} and r.json()["persona_id"] is None
        ink, _ = _persona_with_account(conn, "Inkwolf")
        r = client.post("/api/artists", json={"name": "Inkwolf", "persona_id": ink})
        assert r.json()["persona_id"] == ink
        r = client.get(f"/api/artists/by-persona/{ink}")
        assert r.status_code == 200 and r.json()["artist"]["name"] == "Inkwolf"
        assert client.get("/api/artists").json()["personas"][0]["name"] == "Inkwolf"


# ── review pass: the edges the first cut missed ──────────────────────────────

class TestReviewEdges:
    def test_a_rename_keeps_the_persona_link_and_follows_the_key_onto_the_pieces(self, client, conn, archive):
        """A re-key that left the old key behind would make every future post
        fall back to the snapshot — corrections and the persona link lost
        silently — and people[] rows on OTHER pieces never meet the by-artist
        scan, so they are rewritten too."""
        ink, _ = _persona_with_account(conn, "Inkwolf")
        old = aq.upsert_artist(conn, "Inkwolf", handles={"fa": "inkwolf"}, persona_id=ink, mention={"fa": True})
        conn.commit()
        _piece(archive, "Drawn", artist={"key": old, "name": "Inkwolf", "handles": {"fa": "inkwolf"}})
        _piece(archive, "Featuring", artist=None, characters=["Alpha"],
               people=[{"key": old, "role": "owner", "character": "Alpha"}])
        r = client.post(f"/api/artists/{old}/rename", json={"new_name": "Ink Wolf Prime", "apply": True})
        assert r.status_code == 200, r.text
        new = r.json()["key"]
        assert new != old and r.json()["rekeyed"] is True
        row = aq.get_artist(conn, new)
        assert row["persona_id"] == ink and row["mention"] == {"fa": True}
        assert ar.load_artwork("Drawn").artist == {"key": new, "name": "Ink Wolf Prime", "handles": {"fa": "inkwolf"}}
        assert ar.load_artwork("Featuring").people == [{"key": new, "role": "owner", "character": "Alpha"}]
        assert sorted(r.json()["works_updated"]) == ["Drawn", "Featuring"]

    def test_bad_ids_are_400s_not_500s(self, client, conn, archive):
        _piece(archive, "Bad")
        r = client.patch("/api/masterpieces/Bad", json={"artist": None, "artist_status": "own", "own_persona_id": "abc"})
        assert r.status_code == 400
        r = client.post("/api/artists", json={"name": "SecondFur", "persona_id": "abc"})
        assert r.status_code == 400
        r = client.post("/api/artists", json={"name": "SecondFur", "persona_id": "2"})
        assert r.status_code == 200 and r.json()["persona_id"] == 2, "a numeric string is fine"
        r = client.post("/api/artists", json={"name": "SecondFur", "persona_id": 0})
        assert r.json()["persona_id"] is None, "0 / '' / null all clear"

    def test_a_bare_name_is_cleaned_for_the_sites_markup(self):
        person = {"name": "Brack[et] <Fur>", "handles": {"fa": "x"}, "mention": {}}
        assert ac.featuring([_p("commissioner", person)], "fa") == "for Bracket <Fur>"
        assert ac.featuring([_p("commissioner", person)], "da") == "for Brack[et] Fur"
        assert ac.featuring([_p("commissioner", person)], "e621") == "for Brack[et] <Fur>"

    def test_a_linked_person_with_the_handle_on_another_site_only_stays_bare(self):
        person = {"name": "SecondFur", "handles": {"fa": "secondfur"}, "mention": {"ib": True, "fa": True}}
        assert ac.featuring([_p("collaborator", person)], "ib") == "with SecondFur", "consent needs a handle for THAT site"
        assert ac.featuring([_p("collaborator", person)], "fa") == "with :iconsecondfur:"

    def test_people_without_an_artist_still_get_their_line(self, conn, archive):
        k = aq.upsert_artist(conn, "SecondFur", handles={"fa": "secondfur"})
        conn.commit()
        _piece(archive, "Orphan", artist_status="unknown", people=[{"key": k, "role": "collaborator"}])
        pkg = ar.build_artwork_package(ar.load_artwork("Orphan"), "fa")
        assert "\n\nwith SecondFur" in pkg.description and "Art by" not in pkg.description

    def test_a_persona_link_typed_as_a_string_or_bool_is_normalised(self, conn):
        key = aq.upsert_artist(conn, "Inkwolf", persona_id="3")
        assert aq.get_artist(conn, key)["persona_id"] == 3
        aq.upsert_artist(conn, "Inkwolf", persona_id="")
        assert aq.get_artist(conn, key)["persona_id"] is None

    def test_mention_for_a_site_with_no_handle_is_a_no_op(self, conn):
        key = aq.upsert_artist(conn, "SecondFur", handles={"fa": "secondfur"}, mention={"tw": True, "fa": True})
        assert aq.get_artist(conn, key)["mention"] == {"fa": True}


# ── source contracts ─────────────────────────────────────────────────────────

class TestSources:
    def test_the_manager_hands_the_posting_account_to_the_package(self):
        src = open("posting/manager.py", encoding="utf-8").read()
        assert src.count("build_artwork_package(") == 2
        assert src.count("account_id=account_id") >= 2

    def test_the_page_is_people_and_the_picker_knows_roles_and_personas(self):
        html = open("frontend/index.html", encoding="utf-8").read()
        assert '<span class="nav-label">People</span>' in html
        picker = open("frontend/js/artist_picker.js", encoding="utf-8").read()
        for needle in ("data-ap-role", "data-ap-persona", "data-ap-char", "persona_id"):
            assert needle in picker, needle
        mp = open("frontend/js/masterpieces.js", encoding="utf-8").read()
        assert "data-mp-people-add" in mp and "data-mp-people-x" in mp and "own_persona_id" in mp
        page = open("frontend/js/artists.js", encoding="utf-8").read()
        assert "data-ar-mention" in page and "data-ar-persona" in page
