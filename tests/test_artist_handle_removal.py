"""Removing a wrong handle from the registry (3.10.2).

`upsert_artist` MERGES — deliberately, so a handle added by hand for a platform
the lookup never resolved survives the next import. The consequence is that
**clearing a field cannot express "this handle was wrong"**: the merge keeps it,
and since the picker prefills from the registry, it reappears on the next open.

Measured before the fix:

    start          : {'da': 'WRONG_ONE', 'fa': 'right'}
    after a CHANGE : {'da': 'fixed',     'fa': 'right'}     <- edits do land
    after a CLEAR  : {'da': 'fixed',     'fa': 'right'}     <- removal does not

The lookup found several handles that must be removable rather than merely
overwritten — `deviantart.com/cherrykid` and `deviantart.com/quillfox` are empty
look-alikes, and `raxkiyamto` is a 404 typo. Removal is therefore its own route
and its own deliberate button, never a side effect of blanking a field:
losing a credit is worse than carrying a stale handle.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from database.db import get_connection
from database import artist_queries as aq


@pytest.fixture()
def client():
    from dashboard import app
    return TestClient(app)


def test_an_edit_reaches_the_registry_but_a_blank_does_not_remove():
    """Pins the merge semantics this fix exists because of — if a future change
    made blanks destructive, an import carrying only a name would wipe handles."""
    conn = get_connection()
    aq.upsert_artist(conn, "MergePin", handles={"fa": "right", "da": "wrong"})
    aq.upsert_artist(conn, "MergePin", handles={"fa": "right"})     # da cleared
    conn.commit()
    assert aq.find_by_name(conn, "MergePin")["handles"] == {"fa": "right", "da": "wrong"}
    conn.close()


def test_a_handle_can_be_removed_through_the_route(client):
    conn = get_connection()
    key = aq.upsert_artist(conn, "RemoveMe", handles={"fa": "keep", "da": "lookalike"})
    conn.commit()
    conn.close()

    r = client.delete(f"/api/artists/{key}/handles/da")
    assert r.status_code == 200, r.text
    assert r.json()["handles"] == {"fa": "keep"}, "the response carries the new state"

    conn = get_connection()
    assert aq.get_artist(conn, key)["handles"] == {"fa": "keep"}
    conn.close()


def test_removing_a_handle_that_is_not_there_is_harmless(client):
    """The picker only offers ✕ on stored handles, but a double-click or a stale
    modal must not 500."""
    conn = get_connection()
    key = aq.upsert_artist(conn, "IdempotentRemove", handles={"fa": "x"})
    conn.commit()
    conn.close()
    assert client.delete(f"/api/artists/{key}/handles/ws").status_code == 200
    assert client.delete(f"/api/artists/{key}/handles/ws").status_code == 200


def test_removing_from_an_unknown_artist_is_a_404(client):
    assert client.delete("/api/artists/nosuchartistxyz/handles/fa").status_code == 404


def test_the_artist_itself_survives_losing_every_handle(client):
    """A name-only credit is still a valid credit — `artist_credit` degrades to a
    plain "Art by <name>" line. Eight of the 44 artists are already name-only."""
    conn = get_connection()
    key = aq.upsert_artist(conn, "NameOnlyNow", handles={"fa": "gone"})
    conn.commit()
    conn.close()

    client.delete(f"/api/artists/{key}/handles/fa")

    conn = get_connection()
    a = aq.get_artist(conn, key)
    assert a is not None, "the artist must not be deleted along with their handles"
    assert a["handles"] == {}
    conn.close()


def test_the_frontend_calls_the_route_it_needs():
    """A remove button wired to nothing would look like it worked."""
    import pathlib
    js = pathlib.Path("frontend/js/artist_picker.js").read_text(encoding="utf-8")
    api = pathlib.Path("frontend/js/api.js").read_text(encoding="utf-8")
    assert "deleteArtistHandle" in js
    assert "deleteArtistHandle" in api
    assert "data-ap-rmhandle" in js
