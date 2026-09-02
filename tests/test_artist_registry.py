"""The artist registry made live (3.10.0).

3.5.0 structured the artist field and taught `artist_credit` to render it per
platform. The August 2026 lookup then verified 44 artists and 134 handles across
ten sites, graded each one, and wrote down both the rejections and the artists
whose terms restrict reposting — then that research was applied ONCE as a
migration payload and left in a workspace file the app could not read.

The visible cost, reported as "if a piece is missing an artist, I should be able
to fill in the artist name": the UI's only advice was *"Add the artist to
masterpiece.json"*, i.e. edit a file on the server. And had it been editable, it
would have meant retyping handles that were already researched.
"""
from __future__ import annotations

import pytest

from database.db import get_connection
from database import artist_queries as aq


# ── identity ─────────────────────────────────────────────────────

def test_spelling_drift_collapses_to_one_artist():
    """The lookup itself hit this — it recorded a `corrected_name` for
    'Dan Cresent Wolf' → 'Dan Crescent Wolf', and the handles spell it
    'DanCrescentWolf'. Three spellings must not become three artists that each
    know a third of the handles."""
    keys = {aq.artist_key(n) for n in
            ("Dan Crescent Wolf", "DanCrescentWolf", "dan crescent wolf",
             "Dan  Crescent-Wolf")}
    assert len(keys) == 1


def test_an_empty_name_is_rejected_rather_than_stored():
    conn = get_connection()
    with pytest.raises(ValueError):
        aq.upsert_artist(conn, "   ")
    conn.close()


# ── storage + merge ──────────────────────────────────────────────

def test_handles_merge_and_do_not_wipe_each_other():
    """A handle typed by hand for a platform the lookup never resolved has to
    survive the next import, or the registry actively destroys work."""
    conn = get_connection()
    aq.upsert_artist(conn, "MergeTest", handles={"fa": "mergetest"})
    aq.upsert_artist(conn, "MergeTest", handles={"e621": "merge_test"})
    conn.commit()
    a = aq.find_by_name(conn, "mergetest")
    assert a["handles"] == {"fa": "mergetest", "e621": "merge_test"}
    conn.close()


def test_replace_handles_is_available_but_opt_in():
    conn = get_connection()
    aq.upsert_artist(conn, "ReplaceTest", handles={"fa": "a", "da": "b"})
    aq.upsert_artist(conn, "ReplaceTest", handles={"fa": "c"}, replace_handles=True)
    conn.commit()
    assert aq.find_by_name(conn, "ReplaceTest")["handles"] == {"fa": "c"}
    conn.close()


def test_updating_a_name_does_not_discard_stored_research():
    """The editor sends name + handles and nothing else. If that blanked flags,
    a single edit would silently drop a repost prohibition."""
    conn = get_connection()
    aq.upsert_artist(conn, "FlagKeeper", handles={"fa": "fk"},
                     flags=["do not repost without asking"], notes="not on da: no account")
    aq.upsert_artist(conn, "FlagKeeper", handles={"fa": "fk2"})   # a plain edit
    conn.commit()
    a = aq.find_by_name(conn, "FlagKeeper")
    assert a["flags"] == ["do not repost without asking"]
    assert "not on da" in a["notes"]
    conn.close()


def test_a_blank_handle_is_not_stored():
    conn = get_connection()
    aq.upsert_artist(conn, "BlankHandle", handles={"fa": "  ", "da": "real"})
    conn.commit()
    assert aq.find_by_name(conn, "BlankHandle")["handles"] == {"da": "real"}
    conn.close()


def test_a_handle_can_be_removed():
    conn = get_connection()
    key = aq.upsert_artist(conn, "DropHandle", handles={"fa": "x", "da": "y"})
    aq.remove_handle(conn, key, "fa")
    conn.commit()
    assert aq.get_artist(conn, key)["handles"] == {"da": "y"}
    conn.close()


# ── lookup ───────────────────────────────────────────────────────

def test_search_finds_an_artist_by_their_handle():
    """Old descriptions credit the handle, not the display name — searching for
    'honeyvanillaa' has to find Azzieworks."""
    conn = get_connection()
    aq.upsert_artist(conn, "SearchTarget", handles={"da": "honeyvanillaa"})
    conn.commit()
    assert "SearchTarget" in [a["name"] for a in aq.list_artists(conn, "honeyvan")]
    conn.close()


def test_resolving_a_loosely_typed_name_returns_the_handles():
    """This is the auto-fill: type it however you remember it, get the verified
    handles back."""
    conn = get_connection()
    aq.upsert_artist(conn, "Racer Dragon", handles={"fa": "racerdragon", "tw": "RacerDrgn"})
    conn.commit()
    a = aq.find_by_name(conn, "racer  dragon")
    assert a and a["handles"]["fa"] == "racerdragon"
    conn.close()


def test_an_unknown_name_resolves_to_nothing_rather_than_a_guess():
    conn = get_connection()
    assert aq.find_by_name(conn, "Nobody At All Xyzzy") is None
    conn.close()


# ── flag classification ──────────────────────────────────────────

@pytest.mark.parametrize("note", [
    "NOT on FurAffinity - /user/inkwolf is user-not-found",
    "their old FurAffinity account /user/zolshii is DEAD - FA returns 'Account disabled'",
    "THEY REBRANDED - '@zolshii' is now their PERSONAL/gaming account",
    "x.com/LindseyVi (no underscores) IS live but is a different handle - do not substitute",
    "TYPO RESOLVED: the recorded FA handle 'raxkiyamto' is a 404",
    "do NOT credit deviantart.com/cherrykid - empty placeholder, not this artist",
])
def test_notes_that_would_cause_a_wrong_credit_are_warnings(note):
    warnings, context = aq.classify_flags([note])
    assert warnings == [note] and context == []


@pytest.mark.parametrize("note", [
    "alias 'Tavi', they/them",
    "Weasyl dormant since 2015, no cross-reference",
    "found from scratch - no seed handle existed",
    "alias Brodie, they/she, Australian",
])
def test_ordinary_context_is_not_alarmed(note):
    warnings, context = aq.classify_flags([note])
    assert context == [note] and warnings == []


def test_classification_actually_discriminates():
    """The whole point. Treating all 90 lookup notes as alarms flagged 44 of 44
    artists, which is identical to flagging none of them."""
    mixed = ["alias 'Tavi', they/them",
             "their FA account is DEAD",
             "Weasyl dormant since 2015"]
    warnings, context = aq.classify_flags(mixed)
    assert len(warnings) == 1
    assert len(context) == 2


def test_blank_notes_are_dropped():
    assert aq.classify_flags(["", "   ", None]) == ([], [])


def test_classification_is_exposed_on_the_artist_record():
    """The editor reads warnings/context off the artist, so they have to be part
    of the record rather than something the caller recomputes."""
    conn = get_connection()
    aq.upsert_artist(conn, "ClassifyMe", flags=["alias Bo", "their DA is DEAD"])
    conn.commit()
    a = aq.find_by_name(conn, "ClassifyMe")
    assert a["warnings"] == ["their DA is DEAD"]
    assert a["context"] == ["alias Bo"]
    conn.close()
