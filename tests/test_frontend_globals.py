"""Every `window.X` the frontend reads must actually exist on window.

A top-level `const X = {...}` in a classic script is a global *binding*, NOT a property of
`window`. So `window.X && X.foo()` is always false and the guarded code silently never runs.
That hid for months: the story Publish panel's persona picker, the drip confirmation, the
story board's live-post links, the editor's unsaved-edits guard, several history charts and
the frontend error reporter were all dead code behind such guards (fixed 4.43.1). The
source-text tests that covered those features passed throughout, because the text was there.

This test is the general rule: if a name is declared top-level with const/let/class, and
anything reads `window.<name>`, some file must assign `window.<name> = ...`.
"""
from __future__ import annotations

import re
from pathlib import Path

JS = Path(__file__).resolve().parent.parent / "frontend" / "js"


def _sources():
    return {p.name: p.read_text(encoding="utf-8") for p in sorted(JS.glob("*.js"))}


def test_no_window_guard_points_at_a_top_level_const():
    src = _sources()
    allsrc = "\n".join(src.values())
    declared = {m.group(1): f for f, s in src.items()
                for m in re.finditer(r"^(?:const|let|class)\s+([A-Z][A-Za-z0-9_]*)\b", s, re.M)}
    assigned = set(re.findall(r"window\.([A-Z][A-Za-z0-9_]*)\s*=(?!=)", allsrc))
    read = {}
    for f, s in src.items():
        for m in re.finditer(r"window\.([A-Z][A-Za-z0-9_]*)\b(?!\s*=(?!=))", s):
            read.setdefault(m.group(1), f)
    broken = sorted(f"window.{n} is read (e.g. {read[n]}) but {n} is a top-level declaration in "
                    f"{declared[n]} and nothing assigns window.{n}"
                    for n in read if n in declared and n not in assigned)
    assert not broken, "\n".join(broken)


def test_the_rule_would_have_caught_the_original_bug():
    """The detector itself: a const read through window with no assignment is flagged."""
    decl = re.findall(r"^(?:const|let|class)\s+([A-Z]\w*)", "const Utils = {\n};\n", re.M)
    reads = re.findall(r"window\.([A-Z]\w*)\b(?!\s*=(?!=))", "if (window.Utils && Utils.x) {}")
    assigns = re.findall(r"window\.([A-Z]\w*)\s*=(?!=)", "if (window.Utils && Utils.x) {}")
    assert decl == ["Utils"] and reads == ["Utils"] and assigns == []
