"""Renaming an artist (3.11.0).

`artist_key` is derived from the name, so a real rename changes the key and the
handle rows have to travel with it. Worse, `masterpiece.json` stores the artist's
name **inline** — there is no foreign key — so renaming in the registry alone
leaves every piece still crediting the old spelling.

That makes a rename two operations that must not drift apart, and the second one
rewrites artwork metadata. It therefore previews first and lists exactly which
pieces it will touch, per the house rule that bulk artwork edits are shown before
they happen.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from database.db import get_connection
from database import artist_queries as aq
from posting import artwork_reader


@pytest.fixture()
def client():
    from dashboard import app
    return TestClient(app)


@pytest.fixture()
def archive(tmp_path, monkeypatch):
    root = tmp_path / "artwork"
    root.mkdir()
    monkeypatch.setattr(artwork_reader, "get_artwork_archive_path", lambda: root)
    return root


def _work(root, name, artist_name):
    f = root / name
    f.mkdir()
    (f / "image.jpg").write_bytes(b"\xff\xd8\xff\xd9")
    (f / "masterpiece.json").write_text(json.dumps({
        "title": name, "rating": "general", "image": "image.jpg",
        "artist": {"name": artist_name, "handles": {"fa": "x"}},
    }), encoding="utf-8")
    return name


# ── the registry half ────────────────────────────────────────────

def test_a_rename_carries_the_handles_to_the_new_key():
    conn = get_connection()
    key = aq.upsert_artist(conn, "Dan Cresent Wolf",
                           handles={"fa": "crescent0100", "ib": "DanCrescentWolf"})
    r = aq.rename_artist(conn, key, "Dan Crescent Wolf")
    conn.commit()

    assert r["rekeyed"] is True
    assert aq.get_artist(conn, key) is None, "the old key must not linger"
    moved = aq.find_by_name(conn, "Dan Crescent Wolf")
    assert moved["handles"] == {"fa": "crescent0100", "ib": "DanCrescentWolf"}
    conn.close()


def test_the_old_name_survives_as_an_alias():
    """Descriptions credit whatever spelling was used at the time, so dropping
    the old name breaks the search that finds them."""
    conn = get_connection()
    key = aq.upsert_artist(conn, "Zolshii", handles={"fa": "zolshii"})
    aq.rename_artist(conn, key, "SNAPPAKAPPA")
    conn.commit()

    a = aq.find_by_name(conn, "SNAPPAKAPPA")
    assert "Zolshii" in a["aliases"]
    assert "SNAPPAKAPPA" in [x["name"] for x in aq.list_artists(conn, "zolshii")]
    conn.close()


def test_a_respelling_that_normalises_the_same_is_display_only():
    """'cherry_kid' -> 'Cherry Kid' is the SAME key. Treating it as a re-key
    would delete and reinsert the row for no reason."""
    conn = get_connection()
    key = aq.upsert_artist(conn, "cherry_kid", handles={"fa": "cherryblossomkid"})
    r = aq.rename_artist(conn, key, "Cherry Kid")
    conn.commit()

    assert r["rekeyed"] is False
    assert r["key"] == key
    a = aq.get_artist(conn, key)
    assert a["name"] == "Cherry Kid"
    assert a["handles"] == {"fa": "cherryblossomkid"}
    conn.close()


def test_research_survives_a_rename():
    """Flags and notes are the lookup's output; a rename must not be a way to
    quietly lose a repost prohibition."""
    conn = get_connection()
    key = aq.upsert_artist(conn, "FlagCarrier", handles={"fa": "fc"},
                           flags=["REPOST POLICY (binding)"], notes="not on da: none found")
    aq.rename_artist(conn, key, "Flag Carrier Renamed")
    conn.commit()

    a = aq.find_by_name(conn, "Flag Carrier Renamed")
    assert a["flags"] == ["REPOST POLICY (binding)"]
    assert "not on da" in a["notes"]
    conn.close()


def test_renaming_onto_an_existing_artist_is_refused():
    """A merge decides whose name, flags and conflicting handles win. Guessing
    that destroys research, so it is refused and reported instead."""
    conn = get_connection()
    a = aq.upsert_artist(conn, "First Artist", handles={"fa": "one"})
    aq.upsert_artist(conn, "Second Artist", handles={"fa": "two"})
    conn.commit()
    with pytest.raises(aq.ArtistExists):
        aq.rename_artist(conn, a, "Second Artist")
    conn.close()


def test_an_empty_new_name_is_refused():
    conn = get_connection()
    key = aq.upsert_artist(conn, "Renameable")
    with pytest.raises(ValueError):
        aq.rename_artist(conn, key, "   ")
    conn.close()


# ── the works half ───────────────────────────────────────────────

def test_the_preview_lists_the_pieces_before_anything_changes(client, archive):
    conn = get_connection()
    key = aq.upsert_artist(conn, "Preview Artist", handles={"fa": "pa"})
    conn.commit()
    conn.close()
    _work(archive, "PieceOne", "Preview Artist")
    _work(archive, "PieceTwo", "Preview Artist")
    _work(archive, "NotTheirs", "Someone Else")

    r = client.post(f"/api/artists/{key}/rename", json={"new_name": "Preview Renamed"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "preview"
    assert sorted(body["works"]) == ["PieceOne", "PieceTwo"]
    # Nothing may have moved yet.
    conn = get_connection()
    assert aq.get_artist(conn, key)["name"] == "Preview Artist"
    conn.close()
    assert artwork_reader.load_artwork("PieceOne").artist["name"] == "Preview Artist"


def test_applying_rewrites_the_credit_on_every_piece(client, archive):
    """The point. Renaming only the registry leaves the works crediting the old
    spelling, because the name is stored inline on each masterpiece.json."""
    conn = get_connection()
    key = aq.upsert_artist(conn, "Old Spelling", handles={"fa": "os"})
    conn.commit()
    conn.close()
    _work(archive, "WorkA", "Old Spelling")
    _work(archive, "WorkB", "Old Spelling")

    r = client.post(f"/api/artists/{key}/rename",
                    json={"new_name": "New Spelling", "apply": True})
    assert r.status_code == 200, r.text
    assert sorted(r.json()["works_updated"]) == ["WorkA", "WorkB"]
    assert r.json()["works_failed"] == []

    for w in ("WorkA", "WorkB"):
        art = artwork_reader.load_artwork(w).artist
        assert art["name"] == "New Spelling"
        assert art["handles"]["fa"] == "x", "the piece's own handles are untouched"


def test_a_conflicting_rename_is_a_409_and_changes_nothing(client, archive):
    conn = get_connection()
    key = aq.upsert_artist(conn, "Conflict Source", handles={"fa": "cs"})
    aq.upsert_artist(conn, "Conflict Target", handles={"fa": "ct"})
    conn.commit()
    conn.close()
    _work(archive, "ConflictWork", "Conflict Source")

    r = client.post(f"/api/artists/{key}/rename",
                    json={"new_name": "Conflict Target", "apply": True})
    assert r.status_code == 409
    assert artwork_reader.load_artwork("ConflictWork").artist["name"] == "Conflict Source"


def test_renaming_an_unknown_artist_is_a_404(client):
    r = client.post("/api/artists/nosuchartistxyz/rename", json={"new_name": "Whoever"})
    assert r.status_code == 404


def test_a_rename_with_no_pieces_still_works(client, archive):
    conn = get_connection()
    key = aq.upsert_artist(conn, "Unused Artist")
    conn.commit()
    conn.close()
    r = client.post(f"/api/artists/{key}/rename",
                    json={"new_name": "Unused Renamed", "apply": True})
    assert r.status_code == 200, r.text
    assert r.json()["works_updated"] == []


# ── work counts ──────────────────────────────────────────────────

def test_counts_are_opt_in(client, archive):
    """The picker opens on a keystroke and must not read 162 folders to do it."""
    conn = get_connection()
    aq.upsert_artist(conn, "Counted Artist")
    conn.commit()
    conn.close()
    _work(archive, "CountedWork", "Counted Artist")

    plain = client.get("/api/artists").json()["artists"]
    assert all("works" not in a for a in plain)

    counted = client.get("/api/artists", params={"with_counts": "true"}).json()["artists"]
    got = next(a for a in counted if a["name"] == "Counted Artist")
    assert got["works"] == 1


def test_counts_match_on_a_differently_spelled_credit(client, archive):
    """The count keys on the normalised name, so a piece crediting 'dan crescent
    wolf' still counts towards 'Dan Crescent Wolf'."""
    conn = get_connection()
    aq.upsert_artist(conn, "Dan Crescent Wolf")
    conn.commit()
    conn.close()
    _work(archive, "SpelledOddly", "dan  crescent-wolf")

    counted = client.get("/api/artists", params={"with_counts": "true"}).json()["artists"]
    got = next(a for a in counted if a["name"] == "Dan Crescent Wolf")
    assert got["works"] == 1
