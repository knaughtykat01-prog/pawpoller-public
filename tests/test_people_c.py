"""People phase (c) leftovers (4.43.1, PEOPLEC) — docs/specs/people_registry.md §2.3, §2.5, §4 Q4.

  * Bluesky: a registry handle's DID is stored on first resolve and mentioned by — a
    handle drifts (new PDS, a bought domain), a DID does not;
  * X photo tags: the piece's people whose X mention switch is on are tagged on the image,
    only when the piece's "Tag people in the photo" option is on;
  * the self-link: on an announcer, a piece you drew can name you — off unless set.
"""
from __future__ import annotations

import asyncio

from database import artist_queries as aq
from posting import artwork_reader as ar
from tests.test_people import INK, SECOND, THIRD, _persona_with_account, _piece, archive, conn  # noqa: F401
from tests.test_x_artwork_announce import _pkg, tw  # noqa: F401


# ── Bluesky DIDs ─────────────────────────────────────────────────────────────

def test_a_did_is_stored_once_and_forgotten_when_the_handle_is_edited(conn):
    key = aq.upsert_artist(conn, "SecondFur", handles={"bsky": "@second.fur.example"})
    conn.commit()
    assert aq.remember_bsky_did(conn, "Second.Fur.Example", "did:plc:aaa") == 1
    assert aq.remember_bsky_did(conn, "second.fur.example", "did:plc:bbb") == 0      # first one wins
    assert aq.bsky_dids(conn, ["@second.fur.example", "stranger.example"]) == {"second.fur.example": "did:plc:aaa"}
    aq.upsert_artist(conn, "SecondFur", handles={"bsky": "@second.fur.example"})     # same handle: kept
    conn.commit()
    assert aq.bsky_dids(conn, ["second.fur.example"])
    aq.upsert_artist(conn, "SecondFur", handles={"bsky": "moved.example"})           # edited: forgotten
    conn.commit()
    assert aq.bsky_dids(conn, ["moved.example"]) == {}
    assert key


def test_a_mention_uses_the_stored_did_and_needs_no_lookup(conn, monkeypatch):
    from clients.bsky.client import BskyClient
    aq.upsert_artist(conn, "SecondFur", handles={"bsky": "second.fur.example"})
    aq.remember_bsky_did(conn, "second.fur.example", "did:plc:stored")
    conn.commit()
    client = BskyClient.__new__(BskyClient)
    looked_up = []

    async def resolve(h):
        looked_up.append(h)
        return None                                        # the handle has moved: no longer resolves

    monkeypatch.setattr(client, "resolve_handle", resolve)
    facets = asyncio.run(client._build_mention_facets("art by @second.fur.example", ["second.fur.example"]))
    assert facets and facets[0]["features"][0]["did"] == "did:plc:stored" and looked_up == []


def test_a_fresh_resolve_is_remembered_for_a_registry_handle(conn, monkeypatch):
    from clients.bsky.client import BskyClient
    aq.upsert_artist(conn, "SecondFur", handles={"bsky": "second.fur.example"})
    conn.commit()
    client = BskyClient.__new__(BskyClient)

    async def resolve(h):
        return "did:plc:fresh"

    monkeypatch.setattr(client, "resolve_handle", resolve)
    asyncio.run(client._build_mention_facets("@second.fur.example", ["second.fur.example"]))
    assert aq.bsky_dids(conn, ["second.fur.example"]) == {"second.fur.example": "did:plc:fresh"}


# ── X photo tags ─────────────────────────────────────────────────────────────

def test_only_consented_x_handles_reach_the_package(conn, archive):
    k2 = aq.upsert_artist(conn, "SecondFur", handles={"tw": "@SecondFur"}, mention={"tw": True})
    k3 = aq.upsert_artist(conn, "ThirdFur", handles={"tw": "ThirdFur"})                # mention off
    conn.commit()
    _piece(archive, "Tagged", artist=INK, people=[{"key": k2, "role": "commissioner"},
                                                  {"key": k3, "role": "collaborator"}])
    pkg = ar.build_artwork_package(ar.load_artwork("Tagged"), "tw")
    assert pkg.extra.get("tw_photo_tags") == ["SecondFur"]
    assert "tw_photo_tags" not in ar.build_artwork_package(ar.load_artwork("Tagged"), "bsky").extra


def test_the_poster_tags_only_when_the_option_is_on(tw):
    poster, made, img = tw
    asyncio.run(poster.post(_pkg(file_path=img, extra={"tw_photo_tags": ["SecondFur", "Nobody"]})))
    assert made[0].tagged is None and not any(c[0] == "lookup" for c in made[0].calls)   # off by default
    asyncio.run(poster.post(_pkg(file_path=img, extra={"tw_photo_tags": ["SecondFur", "Nobody"],
                                                       "phototags": True})))
    assert made[1].tagged == ["111"]                        # an unknown handle is skipped, not fatal


def test_the_client_caps_tags_at_ten():
    src = open("clients/tw/client.py", encoding="utf-8").read()
    assert '"tagged_users": tagged' in src and "[:10]" in src


# ── the self-link ────────────────────────────────────────────────────────────

def test_a_self_drawn_piece_names_you_on_an_announcer_only_when_asked(conn, archive):
    ink, tg = _persona_with_account(conn, "Inkwolf", platform="tg")
    key = aq.upsert_artist(conn, "Inkwolf", handles={"tg": "inkwolf"}, persona_id=ink)
    conn.commit()
    _piece(archive, "Mine_Tg", artist={"key": key, **INK})
    pkg = ar.build_artwork_package(ar.load_artwork("Mine_Tg"), "tg", account_id=tg)
    assert "Art by" not in pkg.description                                  # off by default
    _piece(archive, "Mine_Tg_On", artist={"key": key, **INK}, categories={"tg": {"selfcredit": True}})
    pkg = ar.build_artwork_package(ar.load_artwork("Mine_Tg_On"), "tg", account_id=tg)
    assert "Art by" in pkg.description and "inkwolf" in pkg.description


def test_the_self_link_never_reaches_a_gallery_site(conn, archive):
    ink, e6 = _persona_with_account(conn, "Inkwolf")
    key = aq.upsert_artist(conn, "Inkwolf", handles=INK["handles"], persona_id=ink)
    conn.commit()
    _piece(archive, "Mine_E6", artist={"key": key, **INK}, categories={"e621": {"selfcredit": True}})
    pkg = ar.build_artwork_package(ar.load_artwork("Mine_E6"), "e621", account_id=e6)
    assert "Art by" not in pkg.description


def test_every_new_option_has_a_control_and_a_settings_key():
    js = open("frontend/js/artwork.js", encoding="utf-8").read()
    app = open("frontend/js/app.js", encoding="utf-8").read()
    api = open("routes/api.py", encoding="utf-8").read()
    assert js.count("['selfcredit',") == 3 and "['phototags'," in js
    assert app.count("['selfcredit',") == 3 and "['phototags'," in app
    assert '"phototags", "selfcredit"' in api and api.count('"selfcredit"') == 3
