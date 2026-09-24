"""Every `API.something(...)` the frontend calls actually exists in `api.js`.

⚠ **Why this file exists.** The Trello settings panel shipped calling
`API.saveSettings(...)`, which has never existed in this codebase — it was
invented while writing the panel and nothing noticed. The failure was quiet in
the worst way: "Load my boards" threw a `TypeError` before reaching the network,
the surrounding `catch` reported "Could not load your boards", and the operator
was told to check a board list that had never been requested. It shipped in three
releases and was found by a person trying to use it.

The tests that should have caught it could not: they read the panel's *source*
for the strings it contains, and a call to a function that does not exist is a
perfectly ordinary string. Source inspection cannot see an undefined name — only
running it can, or this.

One regex over the frontend is a cheaper guard than a browser, and it generalises:
the same mistake in any other module fails here too.
"""
from __future__ import annotations

import re
from pathlib import Path

JS = Path(__file__).resolve().parent.parent / "frontend" / "js"


def _defined() -> set[str]:
    """Names on the API object. Covers both `name() {}` and `name: fn` forms."""
    src = (JS / "api.js").read_text(encoding="utf-8")
    out = set(re.findall(r"^\s{4}(?:async\s+)?(\w+)\s*\(", src, re.M))
    out |= set(re.findall(r"^\s{4}(\w+)\s*:", src, re.M))
    return out


def _calls() -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for f in sorted(JS.glob("*.js")):
        if f.name == "api.js":
            continue
        names = set(re.findall(r"\bAPI\.(\w+)\s*\(", f.read_text(encoding="utf-8")))
        if names:
            out[f.name] = names
    return out


def test_the_api_object_was_found_at_all():
    """A rename that broke the regex would make every other test here vacuous."""
    defined = _defined()
    assert len(defined) > 100, f"only found {len(defined)} helpers — the parse is wrong"
    for known in ("getCommissions", "getTrelloBoards", "getTrelloConfig"):
        assert known in defined


def test_every_frontend_call_resolves_to_a_real_helper():
    defined = _defined()
    missing = {f: sorted(n for n in names if n not in defined)
               for f, names in _calls().items()}
    missing = {f: n for f, n in missing.items() if n}
    assert not missing, f"frontend calls API helpers that do not exist: {missing}"


def test_the_trello_panel_in_particular():
    """Named separately because this is the one that shipped broken, and a
    regression here means the Connect flow is dead again rather than merely
    degraded."""
    defined = _defined()
    src = (JS / "app.js").read_text(encoding="utf-8")
    i = src.index("_drawTrello()")
    panel = src[i:i + 14000]
    for name in set(re.findall(r"\bAPI\.(\w+)\s*\(", panel)):
        assert name in defined, f"the Trello panel calls API.{name}, which does not exist"


def test_saving_the_credentials_has_a_helper_and_a_route():
    """`/boards` reads the STORED key and token, not the form, so something has to
    put them there first. That was the missing link."""
    assert "saveTrelloCredentials" in _defined()
    routes = (Path(__file__).resolve().parent.parent / "routes" / "trello_api.py").read_text(
        encoding="utf-8")
    assert '@trello_router.post("/credentials")' in routes
