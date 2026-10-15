"""MENUZ (4.76.0): the Library's floating filter bar stays below the fixed bar/rail, so the
account menu inside the bar is never covered, and in the top-bar layout it sticks under the bar."""
import re
from pathlib import Path

CSS = Path(__file__).resolve().parents[1] / "frontend" / "css"


def _z(text: str, selector: str) -> int:
    body = text[text.index(selector + " {"):]
    return int(re.search(r"z-index:\s*(\d+)", body[:body.index("}")]).group(1))


def test_the_filter_bar_sits_below_the_bar_that_holds_the_account_menu():
    shelf = (CSS / "bookshelf.css").read_text(encoding="utf-8")
    layout = (CSS / "layout.css").read_text(encoding="utf-8")
    assert _z(shelf, "#shelf-controls") < _z(layout, ".sidebar")


def test_in_the_top_bar_layout_it_sticks_under_the_bar_not_behind_it():
    shelf = (CSS / "bookshelf.css").read_text(encoding="utf-8")
    assert 'html[data-navmode="top"]:not([data-mobile="1"]) #shelf-controls { top: 58px; }' in shelf
