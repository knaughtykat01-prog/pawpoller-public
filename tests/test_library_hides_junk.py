"""Junk is kept-but-HIDDEN — in the Library too (3.13.1).

`junk` has meant "kept but hidden" since 2.149.0: the folder and its members
survive, the grid just stops showing the piece. The Masterpieces grid has
honoured that from the start. The **Library** never did — `/api/works` did not
read `masterpieces.status` at all (the `status` fields it already carried are
*publication* statuses), so junking a piece hid it from one surface and left it
sitting in plain sight on the other.

That is a quiet failure of the promise the feature makes. Junking is the
reversible alternative to deletion precisely because deleting artwork is
forbidden — so if junking does not actually hide, the safe option stops being
useful and the unsafe one starts looking attractive.

Two halves, tested separately: the API has to KNOW (it joins the status in),
and the Library has to HIDE (it filters the cached list).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from routes.submissions_api import assemble_works

ROOT = Path(__file__).resolve().parent.parent
BOOKSHELF = ROOT / "frontend" / "js" / "bookshelf.js"

ARTWORKS = [{"name": "Keep", "title": "Keep"}, {"name": "Trash", "title": "Trash"}]
STORIES = [{"name": "Tale", "title": "Tale"}]


def _works(junk=None):
    return assemble_works(stories=STORIES, artworks=ARTWORKS, pubs=[],
                          acct_to_persona={}, personas={}, junk=junk)["works"]


def _by_name(works):
    return {w["name"]: w for w in works}


# ── the API knows ────────────────────────────────────────────────

def test_junked_artwork_is_flagged():
    w = _by_name(_works({"Trash": "junk", "Keep": ""}))
    assert w["Trash"]["is_junk"] is True
    assert w["Keep"]["is_junk"] is False


def test_a_work_with_no_index_row_is_not_junk():
    """`statuses()` only returns rows that exist in `masterpieces`. A folder
    that was never indexed must default to visible, not vanish."""
    w = _by_name(_works({}))
    assert all(x["is_junk"] is False for x in w.values())


def test_the_flag_is_present_even_with_no_junk_map():
    """Callers that never pass `junk` (older tests, direct use) must still get a
    usable key rather than a KeyError at the consumer."""
    assert all("is_junk" in w for w in _works(None))


def test_stories_are_never_junk():
    """Junk is a Masterpiece concept; a story has no such status. The key is
    still present so a consumer never has to branch on content_type."""
    w = _by_name(_works({"Tale": "junk"}))
    assert w["Tale"]["is_junk"] is False


def test_only_the_exact_status_junk_hides_a_work():
    """Guarding against a truthiness check creeping in — any other status is
    not junk."""
    w = _by_name(_works({"Trash": "archived", "Keep": ""}))
    assert w["Trash"]["is_junk"] is False


def test_junked_works_are_still_returned():
    """The endpoint exposes rather than filters: the Library fetches once and
    filters client-side, so dropping them here would make the Junk view
    unsatisfiable without a second round trip."""
    names = {w["name"] for w in _works({"Trash": "junk"})}
    assert "Trash" in names


# ── the Library hides ────────────────────────────────────────────

def _bookshelf() -> str:
    return BOOKSHELF.read_text(encoding="utf-8")


def test_the_library_filters_on_the_flag():
    src = _bookshelf()
    assert "w.is_junk" in src, "bookshelf.js never reads the junk flag"


def test_junk_is_excluded_by_default_and_included_only_by_the_filter():
    """Both directions matter: the default view drops junk, and the Junk view
    shows *only* junk rather than everything-plus-junk.

    Deliberately NOT pinned to the exact statement shape. The first version of
    this test matched `else list = list.filter(...)` literally and broke the
    moment 3.14.0 added the `status:junk` escape hatch in front of it — the
    behaviour was still right, the regex was just over-fitted. Assert that both
    filters exist; the *guard* on the negative one is covered by
    test_search_query.py, which tests it through the real parser.
    """
    src = _bookshelf()
    assert "list.filter(w => w.is_junk)" in src, "no junk-only path"
    assert "list.filter(w => !w.is_junk)" in src, "junk is never excluded"


def test_the_junk_filter_is_offered_in_the_status_dropdown():
    assert 'value="junk"' in _bookshelf()


def test_the_junk_option_hides_itself_when_nothing_is_junked():
    """An always-present filter for an empty bin is noise — same rule the
    Masterpieces grid uses for its toggle."""
    src = _bookshelf()
    assert re.search(r"junkCount \|\| this\._status === 'junk'", src)


def test_the_empty_bin_says_so():
    """'No works match this filter' reads like a broken filter when the honest
    answer is 'nothing is junked'."""
    assert "The junk bin is empty" in _bookshelf()


def test_junk_hiding_runs_before_the_publish_state_filters():
    """A junked draft must not reappear because you narrowed to Drafts. The
    junk split has to come first in `_filtered`, not as another branch of the
    posted/drafts/unattributed chain."""
    src = _bookshelf()
    junk_at = src.index("w => w.is_junk")
    posted_at = src.index("this._status === 'posted'")
    assert junk_at < posted_at
