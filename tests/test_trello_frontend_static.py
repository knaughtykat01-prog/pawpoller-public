"""The Trello board frontend (spec 006) is wired in, and stays CSP-safe.

Source checks only -- the board's behaviour needs a browser. These catch the
cheap regressions: a script tag dropped from index.html, a route lost from the
router, an inline handler (the CSP blocks it silently), or a 005 helper coming
back after its route was retired.
"""
from __future__ import annotations

import re
from pathlib import Path

FE = Path(__file__).resolve().parent.parent / "frontend"


def _read(rel: str) -> str:
    return (FE / rel).read_text(encoding="utf-8")


def test_index_loads_sortable_before_the_board():
    html = _read("index.html")
    assert "/css/trello_board.css" in html
    i = html.index("/js/vendor/Sortable.min.js")
    assert i < html.index("/js/trello_board.js")
    assert (FE / "js" / "vendor" / "Sortable.min.js").stat().st_size > 10_000
    assert 'href="#/boards"' in html, "the Boards nav entry is missing"


def test_router_has_the_board_routes():
    src = _read("js/app.js")
    assert "TrelloBoard.route(" in src
    assert "TrelloBoard.renderPicker()" in src
    assert "parts[1] === 'list'" in src, "#/commissions/list (the old hub) must still route"


def test_no_inline_handlers_in_the_board():
    src = _read("js/trello_board.js")
    assert not re.search(r"\son[a-z]+\s*=\s*[\"']", src), "inline handler -- the CSP blocks these"


def test_the_retired_005_helpers_are_gone():
    api = _read("js/api.js")
    for name in ("previewTrello(", "syncTrello(", "getTrelloCandidates(", "importTrelloCard(",
                 "unlinkTrello(", "getTrelloLists("):
        assert name not in api, name


def test_deletes_always_carry_confirm():
    """Checklist items, labels and comments are the only things PawPoller may
    delete in Trello, and the route refuses without ?confirm=1 (FR-027)."""
    api = _read("js/api.js")
    for m in re.finditer(r"this\.del\(`/api/trello/(labels|checklists|items|comments)/[^`]*`", api):
        assert "?confirm=1" in m.group(0), m.group(0)
