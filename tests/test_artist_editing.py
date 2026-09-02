"""Filling in a missing artist from the dashboard (3.10.0).

Before this, ``PATCH /api/masterpieces/{name}`` accepted title, description,
rating, characters, alt_text and tags — but not ``artist``. The UI's own advice
was "Add the artist to masterpiece.json", i.e. edit a file on the server. So the
one field that decides what gets credited on every platform was the one field
that could not be edited.

Two things are asserted here: that the field round-trips, and that the registry
does the typing — a name that is already known arrives with its verified handles
attached without the caller supplying them.
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
def work(tmp_path, monkeypatch):
    """A minimal artwork folder the reader will load."""
    root = tmp_path / "artwork"
    folder = root / "ArtistEditTarget"
    folder.mkdir(parents=True)
    (folder / "image.jpg").write_bytes(b"\xff\xd8\xff\xd9")
    (folder / "masterpiece.json").write_text(json.dumps({
        "title": "Artist Edit Target", "description": "d", "rating": "general",
        "image": "image.jpg", "tags": {"core": ["anthro"]},
    }), encoding="utf-8")
    monkeypatch.setattr(artwork_reader, "get_artwork_archive_path", lambda: root)
    return "ArtistEditTarget"


def _read(name):
    return artwork_reader.load_artwork(name)


# ── the field itself ─────────────────────────────────────────────

def test_an_artist_can_be_set_from_the_api(client, work):
    r = client.patch(f"/api/masterpieces/{work}",
                     json={"artist": {"name": "Snejek", "handles": {"fa": "snejek1"}}})
    assert r.status_code == 200, r.text
    art = _read(work).artist
    assert art["name"] == "Snejek"
    assert art["handles"]["fa"] == "snejek1"


def test_a_bare_name_is_enough(client, work):
    """"Fill in the artist name" has to mean exactly that — an object shouldn't
    be required for the commonest case."""
    r = client.patch(f"/api/masterpieces/{work}", json={"artist": "Morgdl"})
    assert r.status_code == 200, r.text
    assert _read(work).artist["name"] == "Morgdl"


def test_clearing_the_artist_corrects_a_wrong_attribution(client, work):
    client.patch(f"/api/masterpieces/{work}", json={"artist": "Wrong Person"})
    assert _read(work).artist is not None
    r = client.patch(f"/api/masterpieces/{work}", json={"artist": None})
    assert r.status_code == 200, r.text
    assert _read(work).artist is None


# ── registry auto-fill ───────────────────────────────────────────

def test_a_known_name_arrives_with_its_verified_handles(client, work):
    """The point of reviving the registry: type the name, get the research.

    Done server-side so it holds for a script or a curl, not only for the UI.
    """
    conn = get_connection()
    aq.upsert_artist(conn, "Rondonu",
                     handles={"fa": "rondonu", "ib": "rondonu", "da": "rondonu"})
    conn.commit()
    conn.close()

    r = client.patch(f"/api/masterpieces/{work}", json={"artist": "rondonu"})
    assert r.status_code == 200, r.text
    handles = _read(work).artist["handles"]
    assert handles["fa"] == "rondonu" and handles["ib"] == "rondonu"


def test_supplied_handles_beat_the_registry(client, work):
    """Whoever is editing is looking at the artist's page; stored research must
    not overwrite what they just read off it."""
    conn = get_connection()
    aq.upsert_artist(conn, "Overridden", handles={"fa": "old_handle"})
    conn.commit()
    conn.close()

    client.patch(f"/api/masterpieces/{work}",
                 json={"artist": {"name": "Overridden", "handles": {"fa": "new_handle"}}})
    assert _read(work).artist["handles"]["fa"] == "new_handle"


def test_an_unknown_artist_is_learned(client, work):
    """The registry has to grow as the catalogue is corrected, or it freezes at
    the original lookup and the next 24 pieces teach it nothing."""
    client.patch(f"/api/masterpieces/{work}",
                 json={"artist": {"name": "Brand New Artist", "handles": {"fa": "bna"}}})
    conn = get_connection()
    try:
        found = aq.find_by_name(conn, "Brand New Artist")
        assert found is not None
        assert found["handles"]["fa"] == "bna"
    finally:
        conn.close()


# ── the three no-artist states ───────────────────────────────────

@pytest.mark.parametrize("status", ["", "own", "unknown"])
def test_artist_status_round_trips(client, work, status):
    r = client.patch(f"/api/masterpieces/{work}", json={"artist_status": status})
    assert r.status_code == 200, r.text
    assert _read(work).artist_status == status


def test_a_nonsense_status_is_rejected(client, work):
    r = client.patch(f"/api/masterpieces/{work}", json={"artist_status": "banana"})
    assert r.status_code == 400


def test_own_work_is_distinguishable_from_a_missing_credit(client, work):
    """The whole reason for the field: the PFP and the Commission_Archive
    folders warned forever because 'drawn by me' and 'nobody filled this in'
    looked identical."""
    client.patch(f"/api/masterpieces/{work}", json={"artist_status": "own"})
    art = _read(work)
    assert art.artist is None
    assert art.artist_status == "own"


def test_an_unreadable_status_falls_back_to_the_warning_state(work):
    """Never let a corrupt value silently suppress the reminder that a piece is
    missing its credit."""
    assert artwork_reader._clean_artist_status("nonsense") == ""
    assert artwork_reader._clean_artist_status(None) == ""
    assert artwork_reader._clean_artist_status("OWN") == "own"


def test_the_detail_payload_carries_the_status(client, work):
    """The editor reads it off the detail response; without it the dropdown
    would always open on the wrong option."""
    client.patch(f"/api/masterpieces/{work}", json={"artist_status": "unknown"})
    r = client.get(f"/api/masterpieces/{work}")
    assert r.status_code == 200, r.text
    assert r.json().get("artist_status") == "unknown"


# ── registry routes ──────────────────────────────────────────────

def test_the_registry_lists_and_resolves(client):
    conn = get_connection()
    aq.upsert_artist(conn, "RouteTest", handles={"fa": "routetest"})
    conn.commit()
    conn.close()

    r = client.get("/api/artists")
    assert r.status_code == 200, r.text
    body = r.json()
    assert any(a["name"] == "RouteTest" for a in body["artists"])
    # The counts are nested, not spread: `count()` has its own "artists" key and
    # merging it replaced the list with an integer.
    assert isinstance(body["totals"]["artists"], int)
    assert "fa" in body["platforms"]

    r = client.get("/api/artists/resolve", params={"name": "route test"})
    assert r.status_code == 200
    assert r.json()["artist"]["handles"]["fa"] == "routetest"


def test_resolving_an_unknown_name_is_not_an_error(client):
    """The editor calls this on every keystroke-ish change; a 404 would turn
    typing a new artist's name into an error state."""
    r = client.get("/api/artists/resolve", params={"name": "Nobody Xyzzy 12345"})
    assert r.status_code == 200
    assert r.json()["artist"] is None


def test_an_artist_can_be_saved_through_the_route(client):
    r = client.post("/api/artists", json={"name": "PostedArtist",
                                          "handles": {"bsky": "posted.bsky.social"}})
    assert r.status_code == 200, r.text
    assert r.json()["handles"]["bsky"] == "posted.bsky.social"


def test_saving_without_a_name_is_rejected(client):
    assert client.post("/api/artists", json={"handles": {"fa": "x"}}).status_code == 400
