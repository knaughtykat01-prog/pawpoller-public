"""Lifting the artist credit out of an artwork description.

The catalogue recorded the artist as free text at the tail of the description,
in six different phrasings, which meant it could not be rendered per platform
(a raw furaffinity.net URL was posted to FurAffinity itself as plain text) and
could not be indexed as a tag. `scripts/extract_artist_credit.py` splits that
tail into a structured field.

The parse is regex over a fixed lead-in list — no model — so these tests pin
the shapes that actually occur in the archive. The important negative case is
the last group: an ordinary blurb sentence containing the word "by" must NOT
be mistaken for a credit, because that would silently eat prose.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "extract_artist_credit",
    Path(__file__).resolve().parent.parent / "scripts" / "extract_artist_credit.py")
eac = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(eac)

from posting import artwork_reader as ar  # noqa: E402


def _split(desc):
    return eac.split_description(desc)


# --------------------------------------------------------------- lead-ins
@pytest.mark.parametrize("line,expected", [
    ("Art by Inkwolf", "Inkwolf"),
    ("art by inkwolf", "inkwolf"),
    ("Done by Sablejay", "Sablejay"),
    ("Drawn by Brightmoth", "Brightmoth"),
    ("This was done by Copperfern", "Copperfern"),
    ("Commission by Copperfern", "Copperfern"),
    ("Artwork by Thistledown", "Thistledown"),
    ("Sketch by Emberkite", "Emberkite"),
])
def test_lead_in_phrases(line, expected):
    blurb, artist, raw = _split(f"A blurb.\n\n{line}")
    assert artist["name"] == expected
    assert blurb == "A blurb."
    assert raw == line


def test_longest_lead_in_wins():
    """'this was done by' must not be parsed as the 'done by' inside it."""
    _, artist, _ = _split("Blurb.\n\nThis was done by Copperfern")
    assert artist["name"] == "Copperfern"


# --------------------------------------------------------------- handles
@pytest.mark.parametrize("line,key,handle", [
    ("Art by Inkwolf @ https://www.furaffinity.net/user/inkwolf", "fa", "inkwolf"),
    ("Art by Brightmoth @ https://twitter.com/brightmoth", "tw", "brightmoth"),
    ("Art by Foo @ https://x.com/foo_art", "tw", "foo_art"),
    ("Art by Foo @ https://inkbunny.net/user.php?user=foo", "ib", "foo"),
    ("Art by Foo @ https://www.weasyl.com/~foo", "ws", "foo"),
    ("Art by Foo @ https://www.deviantart.com/foo", "da", "foo"),
    ("Art by Foo @ https://bsky.app/profile/foo.bsky.social", "bsky", "foo.bsky.social"),
])
def test_profile_urls_become_handles(line, key, handle):
    _, artist, _ = _split(f"Blurb.\n\n{line}")
    assert artist["handles"][key] == handle


def test_inline_fa_shorthand():
    _, artist, _ = _split("Blurb.\n\nArt by Pinefox @ FA:PINEFOX463ART")
    assert artist["name"] == "Pinefox"
    assert artist["handles"]["fa"] == "PINEFOX463ART"


def test_url_is_not_left_in_the_name():
    _, artist, _ = _split(
        "Blurb.\n\nArt by Meadowlark on Twitter: https://twitter.com/meadowlark")
    assert artist["name"] == "Meadowlark"


# --------------------------------------------------------------- name cleanup
@pytest.mark.parametrize("line,expected", [
    ("Art by the absolutely wonderful Brightmoth", "Brightmoth"),
    ("Art by the ever lovely Duskhare", "Duskhare"),
    ("Art by the Awesome Copperfern", "Copperfern"),
    ("Art by Greyember!", "Greyember"),
    ("Art by Sablejay ~", "Sablejay"),
])
def test_praise_and_punctuation_are_stripped(line, expected):
    _, artist, _ = _split(f"Blurb.\n\n{line}")
    assert artist["name"] == expected


@pytest.mark.parametrize("line,expected", [
    ("Art by Larkspur — featuring Dar", "Larkspur"),
    ("Art by Foxglove @ featuring Penwright", "Foxglove"),
    ("Art by Sablejay, on an AV base", "Sablejay"),
])
def test_subject_notes_are_not_part_of_the_name(line, expected):
    """'featuring X' describes the picture, not the person who drew it."""
    _, artist, _ = _split(f"Blurb.\n\n{line}")
    assert artist["name"] == expected


# --------------------------------------------------------------- loose "by"
def test_loose_by_is_accepted_when_it_carries_a_link():
    _, artist, _ = _split(
        "Blurb.\n\nAn oh so lovely piece by Duskhare @ https://twitter.com/Duskhare_owo")
    assert artist["name"] == "Duskhare"
    assert artist["handles"]["tw"] == "Duskhare_owo"


def test_blurb_sentence_containing_by_is_not_eaten():
    """The regression this guards: prose must never be parsed as a credit."""
    desc = "Pinned between the kegs and left absolutely flooded by the time she got out."
    blurb, artist, raw = _split(desc)
    assert artist is None
    assert blurb == desc
    assert raw == ""


def test_description_with_no_credit_is_untouched():
    desc = "Red briefs doing heroic work. A YCH close-up.\n\nSecond paragraph."
    blurb, artist, raw = _split(desc)
    assert artist is None and blurb == desc


# --------------------------------------------------------------- edge cases
def test_unknown_artist_is_flagged_not_named():
    _, artist, _ = _split("Blurb.\n\nArtist unknown")
    assert artist["unknown"] is True
    assert artist["name"] == ""


def test_empty_description():
    assert _split("") == ("", None, "")


def test_credit_only_description_yields_empty_blurb():
    """Caller must keep the original text — see the --apply guard."""
    blurb, artist, raw = _split("Art by Sablejay")
    assert artist["name"] == "Sablejay"
    assert blurb == ""


# --------------------------------------------------------------- reader field
def _folder(tmp_path, meta):
    d = tmp_path / "Piece"
    d.mkdir()
    (d / "img.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (d / "masterpiece.json").write_text(json.dumps(meta), encoding="utf-8")
    return d


@pytest.mark.parametrize("stored,expected", [
    ({"name": "Inkwolf", "handles": {"fa": "inkwolf"}},
     {"name": "Inkwolf", "handles": {"fa": "inkwolf"}}),
    ("Inkwolf", {"name": "Inkwolf", "handles": {}}),          # bare string
    ({"name": "Inkwolf"}, {"name": "Inkwolf", "handles": {}}),  # no handles key
    ({"name": "  Inkwolf  ", "handles": {"fa": " inkwolf "}},
     {"name": "Inkwolf", "handles": {"fa": "inkwolf"}}),      # whitespace
    ({"name": "Inkwolf", "handles": {"fa": ""}},
     {"name": "Inkwolf", "handles": {}}),                    # blank handle dropped
])
def test_clean_artist_shapes(stored, expected):
    assert ar._clean_artist(stored) == expected


@pytest.mark.parametrize("stored", [None, "", "   ", {}, {"name": ""}, 42, []])
def test_clean_artist_rejects_unusable(stored):
    assert ar._clean_artist(stored) is None


def test_load_artwork_exposes_artist(tmp_path, monkeypatch):
    d = _folder(tmp_path, {
        "title": "A Piece", "image": "img.png", "rating": "adult",
        "artist": {"name": "Inkwolf", "handles": {"fa": "inkwolf"}},
    })
    monkeypatch.setattr(ar, "get_artwork_archive_path", lambda: tmp_path)
    art = ar.load_artwork(d.name)
    assert art.artist == {"name": "Inkwolf", "handles": {"fa": "inkwolf"}}


def test_load_artwork_without_artist_is_none(tmp_path, monkeypatch):
    d = _folder(tmp_path, {"title": "A Piece", "image": "img.png", "rating": "adult"})
    monkeypatch.setattr(ar, "get_artwork_archive_path", lambda: tmp_path)
    assert ar.load_artwork(d.name).artist is None


# ------------------------------------------------- attribution surfaced to the UI
# The Library flags a piece with no artist so it can be fixed before it posts —
# credit is meant to be present on every piece, so "no artist" is a warning
# state rather than a neutral one. There is no JS harness, so these pin the
# backend flags the frontend reads.

def test_list_artworks_carries_the_artist(tmp_path, monkeypatch):
    d = tmp_path / "WithArtist"
    d.mkdir()
    (d / "img.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (d / "masterpiece.json").write_text(json.dumps({
        "title": "With Artist", "image": "img.png", "rating": "adult",
        "artist": {"name": "Inkwolf", "handles": {"fa": "inkwolf"}},
    }), encoding="utf-8")
    bare = tmp_path / "NoArtist"
    bare.mkdir()
    (bare / "img.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (bare / "masterpiece.json").write_text(json.dumps({
        "title": "No Artist", "image": "img.png", "rating": "adult",
    }), encoding="utf-8")
    monkeypatch.setattr(ar, "get_artwork_archive_path", lambda: tmp_path)

    got = {a["title"]: a for a in ar.list_artworks()}
    assert got["With Artist"]["artist"] == {"name": "Inkwolf", "handles": {"fa": "inkwolf"}}
    assert got["No Artist"]["artist"] is None


@pytest.mark.parametrize("artist,needs,shown", [
    ({"name": "Inkwolf", "handles": {"fa": "x"}}, False, "Inkwolf"),
    (None, True, ""),
    ({}, True, ""),
    ({"handles": {"fa": "x"}}, True, ""),   # a handle with no name is not a credit
])
def test_works_projection_flags_missing_attribution(artist, needs, shown):
    """`needs_artist` is what the Library filter and the card badge read.

    Driven through the real `assemble_works`, which exists precisely to be
    unit-testable over already-fetched data.
    """
    from routes import submissions_api as sa
    out = sa.assemble_works(
        stories=[], artworks=[{"name": "P", "title": "A Piece", "artist": artist}],
        pubs=[], acct_to_persona={}, personas={}, type="artwork",
    )
    work = out["works"][0]
    assert work["needs_artist"] is needs
    assert work["artist_name"] == shown


def test_a_story_is_never_flagged_for_a_missing_artist():
    """Stories have an author, not an artist — flagging them would be noise."""
    from routes import submissions_api as sa
    out = sa.assemble_works(
        stories=[{"name": "S", "title": "A Story"}], artworks=[], pubs=[],
        acct_to_persona={}, personas={}, type="all",
    )
    works = out["works"]
    story = next(w for w in works if w["content_type"] == "story")
    assert "needs_artist" not in story or not story["needs_artist"]
