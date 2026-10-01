"""Library: one card per piece — the page (4.47.0, spec 014).

Drives the real `bookshelf.js` through node (same approach as
test_search_query.py) rather than grepping it: "the toggle exists" proves
nothing about whether a 12-chapter story shows four tiles and "+8 more", or
whether searching for a version's name finds its piece.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SHELF = ROOT / "frontend" / "js" / "bookshelf.js"
SQ = ROOT / "frontend" / "js" / "search_query.js"

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is needed")

PIECE = {
    "content_type": "artwork", "name": "Harbour_Lights", "title": "Harbour Lights",
    "rating": "general", "platforms": ["fa", "ib"], "publication_count": 2,
    "stats": {"views": 1200, "favorites": 80, "comments": 3}, "persona_ids": [],
    "thumb_url": "/api/artwork/image?name=Harbour_Lights&file=a.png&w=400",
    "detail_route": "#/artwork/image/Harbour_Lights", "tags": [], "is_junk": False,
    "needs_artist": False, "status": {"state": "live", "label": "Live on 2", "next_at": ""},
    "variants": [
        {"key": "nsfw", "label": "NSFW", "rating": "adult", "thumb_url": "/x", "platforms": ["fa"],
         "stats": {"views": 300, "favorites": 20, "comments": 0}, "status": "live", "scheduled_at": "",
         "detail_route": "#/artwork/image/Harbour_Lights?v=nsfw"},
        {"key": "sketch", "label": "Sketch", "rating": "general", "thumb_url": "/y", "platforms": [],
         "stats": {"views": 0, "favorites": 0, "comments": 0}, "status": "draft", "scheduled_at": "",
         "detail_route": "#/artwork/image/Harbour_Lights?v=sketch"},
    ],
}
PLAIN = {**PIECE, "name": "Plain", "title": "Plain", "variants": [],
         "status": {"state": "draft", "label": "Draft", "next_at": ""}, "platforms": []}
STORY = {
    "content_type": "story", "name": "Long_Tale", "title": "Long Tale", "rating": "adult",
    "platforms": ["ao3"], "publication_count": 9, "stats": {"views": 900}, "persona_ids": [],
    "tags": [], "is_junk": False, "chapter_count": 12,
    "status": {"state": "live", "label": "Ch 1–9 live", "next_at": "2026-10-06 09:00:00",
               "scheduled_label": "Ch 10–12"},
    "chapters": [{"index": i, "title": f"The Pier {i}", "platforms": ["ao3"] if i < 10 else [],
                  "stats": {"views": 100 - i}, "status": "live" if i < 10 else "scheduled",
                  "scheduled_at": "" if i < 10 else "2026-10-06 09:00:00"} for i in range(1, 13)],
}
ONESHOT = {**STORY, "name": "Short", "title": "Short", "chapters": [], "chapter_count": 1,
           "status": {"state": "live", "label": "Live on 1", "next_at": ""}}
JUNK = {**PLAIN, "name": "Binned", "title": "Binned", "is_junk": True}

PRELUDE = f"""
global.window = global;
global.localStorage = (() => {{ const m = {{}}; return {{
    getItem: k => (k in m ? m[k] : null), setItem: (k, v) => {{ m[k] = String(v); }} }}; }})();
global.Utils = {{
    escapeHtml: s => String(s).replace(/[&<>"']/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c])),
    formatDate: s => String(s), formatCompact: n => String(n), formatNumber: n => String(n),
    _parseDate: s => new Date(String(s).replace(' ', 'T') + 'Z'),
    time: {{ parse: s => new Date(String(s).replace(' ', 'T') + 'Z'),
             format: (d, o) => d.toISOString() }},
}};
require({json.dumps(str(SQ))});
require({json.dumps(str(SHELF))});
const B = window.Bookshelf;
B._personas = [];
"""


def _node(body: str):
    r = subprocess.run(["node", "-e", PRELUDE + textwrap.dedent(body)],
                       capture_output=True, text=True, timeout=60, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def _filter(works, search="", type_="all", status="all"):
    return _node(f"""
        B._works = {json.dumps(works)}; B._type = {json.dumps(type_)};
        B._search = {json.dumps(search)}; B._status = {json.dumps(status)};
        const list = B._filtered();
        process.stdout.write(JSON.stringify({{names: list.map(w => w.name),
            open: [...B._forceOpen]}}));
    """)


def _render(w, parts_all=False):
    return _node(f"""
        B._partsAll = {json.dumps(parts_all)}; B._openMap = {{}}; B._forceOpen = new Set();
        process.stdout.write(JSON.stringify(B._book({json.dumps(w)})));
    """)


# ── one card per piece ───────────────────────────────────────────────────────

@needs_node
def test_a_piece_with_versions_is_one_card_with_hidden_version_tiles():
    html = _render(PIECE)
    assert len(re.findall(r'<div class="book[ "]', html)) == 1          # the card
    tiles = re.findall(r'<a class="book book--part"[^>]*>', html)
    assert len(tiles) == 2
    assert all(" hidden" in t for t in tiles)                            # closed by default
    assert 'data-part-of="artwork:Harbour_Lights"' in html
    assert "?v=nsfw" in html and "?v=sketch" in html


@needs_node
def test_the_toggle_is_a_named_button_that_says_whether_it_is_open():
    html = _render(PIECE)
    btn = re.search(r'<button type="button" class="book-fan[^"]*"[^>]*>', html).group(0)
    assert 'aria-expanded="false"' in btn
    assert 'aria-label="Show 2 versions of Harbour Lights"' in btn
    assert "2 versions" in html
    # It is NOT inside the link (a button in a link is invalid and unreachable).
    link = html.split('<a class="book-link"', 1)[1].split("</a>", 1)[0]
    assert "book-fan" not in link


@needs_node
def test_the_switch_opens_every_card():
    html = _render(PIECE, parts_all=True)
    tiles = re.findall(r'<a class="book book--part"[^>]*>', html)
    assert tiles and not any(" hidden" in t for t in tiles)
    assert 'aria-expanded="true"' in html


@needs_node
def test_a_piece_with_one_version_has_no_toggle_and_no_stack():
    html = _render(PLAIN)
    assert "data-fan=" not in html and "book--stacked" not in html and "book--part" not in html


@needs_node
def test_card_shows_status_numbers_and_sites():
    html = _render(PIECE)
    assert "Live on 2" in html
    assert "1200" in html and "80" in html          # pooled numbers
    tile = html.split("?v=sketch", 1)[1]
    assert "Not posted" in tile                     # a version with no uploads says so


@needs_node
def test_a_long_story_shows_four_chapters_and_more():
    html = _render(STORY)
    assert len(re.findall(r'book--chapter', html)) == 4
    assert "+8 more chapters" in html and "Open the story" in html
    assert "The Pier 1" in html and "The Pier 5" not in html
    assert 'aria-label="Show 12 chapters of Long Tale"' in html
    assert "Ch 1–9 live" in html and "Ch 10–12" in html


@needs_node
def test_a_one_shot_story_has_no_toggle():
    assert "data-fan=" not in _render(ONESHOT)


@needs_node
def test_a_junked_piece_can_be_restored_from_its_card():
    assert 'data-restore="Binned"' in _render(JUNK)


# ── search and filters reach the parts ───────────────────────────────────────

@needs_node
def test_searching_a_versions_name_finds_its_piece_opened():
    got = _filter([PIECE, PLAIN], search="sketch")
    assert got == {"names": ["Harbour_Lights"], "open": ["artwork:Harbour_Lights"]}


@needs_node
def test_a_rating_filter_matches_a_piece_through_its_version():
    got = _filter([PIECE, PLAIN], search="rating:adult")
    assert got["names"] == ["Harbour_Lights"] and got["open"] == ["artwork:Harbour_Lights"]


@needs_node
def test_searching_a_chapter_title_finds_its_story():
    got = _filter([STORY, PIECE], search="pier")
    assert got == {"names": ["Long_Tale"], "open": ["story:Long_Tale"]}


@needs_node
def test_a_piece_matching_on_its_own_is_not_forced_open():
    got = _filter([PIECE], search="harbour")
    assert got == {"names": ["Harbour_Lights"], "open": []}


@needs_node
def test_blocked_storage_leaves_everything_closed():
    got = _node("""
        global.localStorage = { getItem() { throw new Error('blocked'); }, setItem() { throw new Error('blocked'); } };
        B._loadPartsState(); B._openMap['artwork:x'] = true; B._savePartsState();
        process.stdout.write(JSON.stringify({all: B._partsAll}));
    """)
    assert got == {"all": False}


@needs_node
def test_open_state_is_remembered():
    got = _node("""
        B._openMap = {'artwork:Harbour_Lights': true}; B._partsAll = false; B._savePartsState();
        B._openMap = {}; B._loadPartsState();
        process.stdout.write(JSON.stringify({open: B._isOpen('artwork:Harbour_Lights'), other: B._isOpen('artwork:Plain')}));
    """)
    assert got == {"open": True, "other": False}


@needs_node
def test_no_method_shadows_the_librarys_state():
    """A helper once named `_status` replaced the `_status: 'all'` filter field
    (a later key in an object literal wins), leaving the status filter blank.
    Every state field must still be data after the module loads."""
    got = _node("""
        process.stdout.write(JSON.stringify({status: B._status, type: B._type, sort: B._sort,
            search: B._search, platform: B._platform, persona: B._persona}));
    """)
    assert got == {"status": "all", "type": "all", "sort": "recent", "search": "",
                   "platform": "", "persona": 0}


def test_no_duplicate_member_names_in_the_library_module():
    src = _read("frontend/js/bookshelf.js")
    names = re.findall(r"^    (?:async )?(_?[A-Za-z]\w*)\s*(?:\(|:)", src, re.M)
    dupes = sorted({n for n in names if names.count(n) > 1})
    assert not dupes, dupes


# ── the Masterpieces segment is gone ─────────────────────────────────────────

def _read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def test_no_masterpieces_segment_and_old_links_redirect():
    shelf = _read("frontend/js/bookshelf.js")
    assert "seg('masterpiece'" not in shelf
    assert "'masterpiece'" not in shelf.split("TYPES:", 1)[1].split("]", 1)[0]
    app = _read("frontend/js/app.js")
    assert app.count("window.location.replace('#/library/type/artwork')") >= 3   # hub, #/masterpieces, type/masterpiece
    assert "Masterpieces.renderGrid" not in shelf
    assert "renderGrid(" not in _read("frontend/js/masterpieces.js")


def test_the_grid_tools_moved_to_the_library():
    shelf = _read("frontend/js/bookshelf.js")
    assert "#/masterpieces/duplicates" in shelf          # Find duplicates
    assert "#/artwork/new" in shelf                      # New
    assert "data-mp-select" in shelf                      # Select → batch
    assert "API.setMasterpieceStatus(rb.dataset.restore, '')" in shelf   # Restore


def test_reduced_motion_stops_the_slide():
    css = _read("frontend/css/bookshelf.css")
    block = css.split("@media (prefers-reduced-motion: reduce)", 1)[1].split("}\n}", 1)[0]
    assert ".book--part { animation: none; }" in block
