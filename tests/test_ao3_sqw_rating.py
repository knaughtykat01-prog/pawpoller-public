"""AO3RATING: AO3 and SquidgeWorld never rate a story lower than it is.

- A story's rating word maps to the archive's own five words. Their own words map exactly (Teen And Up
  Audiences and Not Rated used to fall through to General Audiences); anything unknown is Explicit.
- An update reads the work's live rating from its edit form and sends whichever is higher, that or the
  story's. It never lowers a live work, and stops when it can't read the live rating.
- The AO3 resume path (a part-posted story picked up again) raises the rating the same way, skin or not.

Stand-in edit pages only; no network.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from clients.otw_forms import OTW_RATING_RANK, RatingUnreadable
from posting.platforms.base import effective_rating


# ── The tables ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("word, level", [
    ("general", "general"), ("General Audiences", "general"), ("g", "general"), ("safe", "general"),
    ("sfw", "general"), ("clean", "general"),
    ("teen", "mature"), ("Teen And Up Audiences", "mature"), ("t", "mature"), ("mature", "mature"),
    ("m", "mature"), ("questionable", "mature"), ("q", "mature"),
    ("explicit", "adult"), ("adult", "adult"), ("e", "adult"), ("nsfw", "adult"), ("x", "adult"),
    ("Not Rated", "adult"), ("", "adult"), (None, "adult"), ("spicy", "adult"), ("  GENERAL  ", "general"),
])
def test_effective_rating_table(word, level):
    assert effective_rating(word) == level


_EXPECTED = {
    "Not Rated": "Not Rated", "General Audiences": "General Audiences",
    "Teen And Up Audiences": "Teen And Up Audiences", "Mature": "Mature", "Explicit": "Explicit",
    "not rated": "Not Rated", " teen and up audiences ": "Teen And Up Audiences",
    "general": "General Audiences", "mature": "Mature", "explicit": "Explicit", "teen": "Teen And Up Audiences",
    "adult": "Explicit", "questionable": "Mature", "sfw": "General Audiences", "spicy": "Explicit", "": "Explicit",
}


@pytest.mark.parametrize("word, site_word", sorted(_EXPECTED.items()))
def test_every_word_maps_ao3(word, site_word):
    from posting.platforms.ao3 import AO3Poster
    assert AO3Poster._rating_to_ao3(word) == site_word


@pytest.mark.parametrize("word, site_word", sorted(_EXPECTED.items()))
def test_every_word_maps_sqw(word, site_word):
    from posting.platforms.squidgeworld import SquidgeWorldPoster
    assert SquidgeWorldPoster._rating_to_sqw(word) == site_word


# ── edit_work on both clients, against a stand-in edit page ────────────────

def _page(live: str | None) -> str:
    if live is None:
        select = ""
    else:
        sel = 'selected="selected" '
        opts = "".join(
            f'<option {sel if w == live else ""}value="{w}">{w}</option>'
            for w in list(OTW_RATING_RANK) + ([live] if live not in OTW_RATING_RANK else [])
        )
        select = f'<select name="work[rating_string]">{opts}</select>'
    return ('<html><form action="/works/123" method="post">'
            '<input type="hidden" name="authenticity_token" value="tok123">'
            '<input type="text" name="work[title]" value="Sample Story">'
            f'{select}</form></html>')


def _sent(body: str) -> str | None:
    from urllib.parse import parse_qsl
    vals = [v for k, v in parse_qsl(body) if k == "work[rating_string]"]
    assert len(vals) <= 1
    return vals[0] if vals else None


def _ao3_client(page: str, posted: dict):
    from clients.ao3.client import AO3Client
    c = object.__new__(AO3Client)
    c._logged_in = True

    async def get_page(url, *a, **k):
        return page

    async def noop(*a, **k):
        return None

    async def post(url, content=None, **k):
        posted["body"] = content
        return SimpleNamespace(status_code=200, text="Work was successfully updated.")

    c._get_page = get_page
    c._polite_delay = noop
    c._post_with_retry = post
    return c


def _sqw_client(page: str, posted: dict, monkeypatch):
    import config
    from clients.sqw.client import SquidgeWorldClient
    monkeypatch.setattr(config, "SQW_REQUEST_DELAY_SECONDS", 0, raising=False)
    c = object.__new__(SquidgeWorldClient)
    c._logged_in = True

    async def get(url, *a, **k):
        return SimpleNamespace(status_code=200, text=page)

    async def post(url, content=None, **k):
        posted["body"] = content
        return SimpleNamespace(status_code=200, text="Work was successfully updated.", url=url)

    c._http = SimpleNamespace(get=get, post=post)
    return c


@pytest.fixture(params=["ao3", "sqw"])
def edit(request, monkeypatch):
    def run(live, wanted):
        posted: dict = {}
        page = _page(live)
        c = _ao3_client(page, posted) if request.param == "ao3" else _sqw_client(page, posted, monkeypatch)
        out = asyncio.run(c.edit_work("123", rating=wanted, raise_only=True))
        assert out["rating_sent"] == _sent(posted["body"])
        return out["rating_sent"]
    return run


def test_update_raises_to_current(edit):
    assert edit("Teen And Up Audiences", "Explicit") == "Explicit"
    assert edit("General Audiences", "Mature") == "Mature"


def test_update_never_lowers(edit):
    assert edit("Explicit", "General Audiences") == "Explicit"
    assert edit("Mature", "Teen And Up Audiences") == "Mature"


def test_not_rated_ordering(edit):
    assert edit("Not Rated", "General Audiences") == "Not Rated"
    assert edit("Not Rated", "Mature") == "Not Rated"
    assert edit("Not Rated", "Explicit") == "Explicit"
    assert edit("Mature", "Not Rated") == "Not Rated"


@pytest.mark.parametrize("live", [None, "", "Spicy"])
def test_unreadable_rating_stops(edit, live):
    with pytest.raises(RatingUnreadable):
        edit(live, "Explicit")


# ── The AO3 resume path ────────────────────────────────────────────────────

@pytest.mark.parametrize("skin_id", ["", "77"])
def test_ao3_resume_raises_rating(skin_id):
    from posting.platforms.ao3 import AO3Poster
    posted: dict = {}
    c = _ao3_client(_page("Teen And Up Audiences"), posted)
    sent = asyncio.run(AO3Poster._resume_edit(c, "123", "Explicit", skin_id))
    assert sent == "Explicit" and _sent(posted["body"]) == "Explicit"


def test_ao3_resume_unreadable_rating_stops():
    from posting.platforms.ao3 import AO3Poster
    c = _ao3_client(_page(None), {})
    with pytest.raises(RatingUnreadable):
        asyncio.run(AO3Poster._resume_edit(c, "123", "Explicit", "77"))


# ── What gets recorded ─────────────────────────────────────────────────────

def test_rating_sent_recorded():
    """The manager records the word the site was sent, when the poster says what it was."""
    from posting import manager
    pkg = SimpleNamespace(rating="adult")
    assert manager._rating_used(pkg, SimpleNamespace(extra={"rating_sent": "Teen And Up Audiences"})) \
        == "Teen And Up Audiences"
    assert manager._rating_used(pkg, SimpleNamespace(extra={})) == "adult"
