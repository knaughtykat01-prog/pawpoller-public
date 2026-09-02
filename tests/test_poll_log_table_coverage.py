"""Every platform's poll-log renderer must exist (3.17.1).

The Polling tab builds its platform list in `app.js` with, per platform, a
`tableFn` naming a function on `Components`. Nothing checked that the named
function was ever written. FurryNetwork shipped as the 18th platform (2.200.0)
with `tableFn: 'fnPollLogTable'` and no such function, so opening Polling threw
`Components[p.tableFn] is not a function`.

The outer `try/catch` then turned a one-row problem into a whole-page one: the
tab rendered "Failed to load polling data" and **every other platform's status
was lost to one missing function.**

This is the same shape as `test_artist_platform_coverage.py` — one fact, several
declarations, no check — and the same fix: pin the declarations to the
definitions so adding a platform without its renderer fails here instead of in
the browser.

Parsed from source rather than executed: these are browser globals with no
module exports, and the failure being guarded is a NAME never defined, which a
text scan settles exactly.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_FRONTEND = Path(__file__).resolve().parent.parent / "frontend" / "js"


def _declared() -> dict[str, str]:
    """{platform key: tableFn name} from the polling tab's platform list."""
    src = (_FRONTEND / "app.js").read_text(encoding="utf-8")
    out = {}
    for m in re.finditer(r"\{\s*key:\s*'([a-z0-9]+)'.*?tableFn:\s*'(\w+)'", src):
        out[m.group(1)] = m.group(2)
    return out


def _defined() -> set[str]:
    """Method names on the Components object literal."""
    src = (_FRONTEND / "components.js").read_text(encoding="utf-8")
    return set(re.findall(r"^\s{4}(\w+PollLogTable)\s*\(", src, re.MULTILINE))


def test_the_platform_list_was_actually_found():
    """Guard the guard: a regex that matches nothing would make every
    assertion below pass vacuously — which is how a bad test hides a bug."""
    declared = _declared()
    assert len(declared) >= 18, f"only found {len(declared)} platforms; regex drifted"
    assert declared.get("fn") == "fnPollLogTable"


def test_every_declared_renderer_exists():
    """The regression. `fn` was declared and never defined."""
    missing = {k: v for k, v in _declared().items() if v not in _defined()}
    assert missing == {}, (
        f"platforms naming a Components function that does not exist: {missing}")


@pytest.mark.parametrize("key", sorted(_declared()))
def test_each_platform_names_a_real_renderer(key):
    """Named per platform so a failure says WHICH one is missing."""
    assert _declared()[key] in _defined()


def test_furrynetworks_renderer_is_present_by_name():
    """Pinned explicitly: this is the one that shipped broken."""
    assert "fnPollLogTable" in _defined()


def test_a_missing_renderer_no_longer_blanks_the_whole_tab():
    """The deeper defect. The render loop called `Components[p.tableFn](...)`
    directly, so one missing function threw into the outer catch and replaced
    the entire tab with "Failed to load polling data". It now resolves through
    a helper that falls back to a per-platform notice."""
    src = (_FRONTEND / "app.js").read_text(encoding="utf-8")
    assert "Components[p.tableFn](" not in src, (
        "call the renderer through _pollLogTable() so a missing one degrades "
        "to a single row instead of taking the tab down")
    assert "_pollLogTable(p)" in src
    assert "typeof fn === 'function'" in src
