"""One platform list, five declarations (3.13.0).

Adding Instagram to the artist registry means touching five separate places:
the registry's `KNOWN_PLATFORMS`, the credit renderer's `PROFILE_URL` and
`_PREFERENCE`, and the platform lists in *both* front-end surfaces (the picker
on a Masterpiece, and the standalone Artists page). Nothing forces them to
agree, and a list that drifts fails silently in the worst way — the field
simply is not offered, so a handle you have cannot be recorded, and nobody sees
an error.

This session produced two bugs of exactly that shape already: a character tag
renamed on disk while the vocabulary kept the dead spelling (3.12.1), and a
retired short name still being offered by the browser (3.12.2). The pattern is
"one fact, several declarations, no check" — so this file is the check.

Note what did NOT need changing: `artist_handles.platform` is a free TEXT
column with no CHECK constraint and `upsert_artist` never filtered against
`KNOWN_PLATFORMS`. Storage always accepted `ig`. These lists govern what the UI
offers, which is why the gap was invisible from the database side.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from database import artist_queries as aq
from database.db import get_connection
from posting import artist_credit as ac

ROOT = Path(__file__).resolve().parent.parent
JS_SURFACES = ("frontend/js/artist_picker.js", "frontend/js/artists.js")


def _js_platforms(relpath: str) -> list[str]:
    """The ['code', 'Label'] pairs from a front-end PLATFORMS list.

    Scans to the matching bracket rather than regexing to the first `],` —
    the list is a list OF lists, so a lazy match stops after one entry and the
    test passes vacuously against a single platform.
    """
    src = (ROOT / relpath).read_text(encoding="utf-8")
    m = re.search(r"PLATFORMS\s*[:=]\s*\[", src)
    assert m, f"no PLATFORMS list found in {relpath}"
    i, depth = m.end() - 1, 0
    for j in range(i, len(src)):
        depth += (src[j] == "[") - (src[j] == "]")
        if depth == 0:
            block = src[i:j + 1]
            break
    else:
        raise AssertionError(f"unterminated PLATFORMS list in {relpath}")
    return re.findall(r"\[\s*'([a-z0-9]+)'\s*,", block)


# ── the lists agree ──────────────────────────────────────────────

@pytest.mark.parametrize("relpath", JS_SURFACES)
def test_every_ui_surface_offers_exactly_the_registry_platforms(relpath):
    """A platform missing from one surface is a handle you can record in the
    picker and not on the Artists page, or the reverse."""
    assert _js_platforms(relpath) == list(aq.KNOWN_PLATFORMS), relpath


def test_both_ui_surfaces_agree_with_each_other():
    a, b = (_js_platforms(p) for p in JS_SURFACES)
    assert a == b


def test_every_platform_can_render_a_profile_link():
    """A handle with no URL template is a credit that silently degrades to bare
    text — the artist gets named but not linked."""
    missing = [p for p in aq.KNOWN_PLATFORMS if p not in ac.PROFILE_URL]
    assert missing == []


def test_every_platform_is_ranked_for_credit_selection():
    """`_pick_handle` falls through `_PREFERENCE`; an unranked platform is only
    ever reached by the arbitrary `next(iter(handles))` fallback."""
    missing = [p for p in aq.KNOWN_PLATFORMS if p not in ac._PREFERENCE]
    assert missing == []


def test_no_declaration_carries_a_platform_the_others_do_not():
    reg = set(aq.KNOWN_PLATFORMS)
    assert set(ac.PROFILE_URL) == reg
    assert set(ac._PREFERENCE) == reg


# ── Instagram specifically ───────────────────────────────────────

def test_instagram_is_offered():
    assert "ig" in aq.KNOWN_PLATFORMS


def test_instagram_renders_a_real_profile_url():
    url = ac.PROFILE_URL["ig"].format(h="scoraart")
    assert url == "https://www.instagram.com/scoraart"


def test_an_instagram_handle_round_trips():
    """The point of the whole change: record it, get it back."""
    conn = get_connection()
    try:
        key = aq.upsert_artist(conn, "Platform Coverage Probe",
                               handles={"ig": "probe_handle"})
        got = aq.find_by_name(conn, "Platform Coverage Probe")
        assert got and got["handles"].get("ig") == "probe_handle"
        aq.remove_handle(conn, key, "ig")
        again = aq.find_by_name(conn, "Platform Coverage Probe")
        assert "ig" not in (again or {}).get("handles", {})
    finally:
        conn.close()


def test_instagram_outranks_e621_but_yields_to_the_furry_sites():
    """Instagram is large but thinly used for furry art, so a credit should
    prefer a site where the work actually lives. It still beats e621, whose
    user pages are booru accounts rather than portfolios."""
    order = list(ac._PREFERENCE)
    assert order.index("ig") < order.index("e621")
    for site in ("fa", "ib", "ws", "sf", "ik", "fn", "da", "tw", "bsky"):
        assert order.index(site) < order.index("ig"), site


def test_a_furry_handle_wins_over_instagram_when_both_exist():
    artist = {"name": "Both", "handles": {"ig": "onlyinsta", "fa": "onfa"}}
    assert ac.profile_url(artist, "bsky").endswith("onfa")


def test_instagram_is_used_when_it_is_all_there_is():
    artist = {"name": "IG Only", "handles": {"ig": "onlyinsta"}}
    assert ac.profile_url(artist, "bsky") == "https://www.instagram.com/onlyinsta"
