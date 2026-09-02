"""CI must never collect the live-posting scripts in `tests/` (3.17.4).

`tests/` holds two unrelated kinds of file:

  * the suite itself, `test_*.py`;
  * one-off operator scripts that talk to LIVE platforms and post real content —
    `bulk_ao3_drafts.py`, `bulk_sf_drafts.py`, `bulk_inkbunny_drafts.py`,
    `edit_sqw_after_fixes.py`, `fa_changestory_canary.py` and friends.

Nothing collected the second group, but only because there was no pytest config
at all and the default glob happened not to match. That was fine while the suite
ran by hand. It stopped being fine when `tests.yml` began running it on **every
push and pull request**: broadening `python_files`, or renaming one script to
`test_*.py`, would have GitHub runners posting drafts to real accounts on every
commit — from datacenter IPs, against a set that includes the FA account
PawPoller must never touch.

`pytest.ini` now pins the glob. These tests pin the pin.
"""
from __future__ import annotations

import configparser
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_TESTS = _ROOT / "tests"

# Scripts whose names alone say they act on live accounts. Not exhaustive —
# the rule below is the real guard — but these are the ones that would hurt.
_LIVE_SCRIPTS = [
    "bulk_ao3_drafts.py", "bulk_sf_drafts.py", "bulk_inkbunny_drafts.py",
    "edit_sqw_after_fixes.py", "fa_changestory_canary.py", "ao3_diagnose.py",
]


def _cfg() -> configparser.ConfigParser:
    cp = configparser.ConfigParser()
    cp.read(_ROOT / "pytest.ini", encoding="utf-8")
    return cp


def test_pytest_ini_exists_and_pins_the_glob():
    """Without a config the glob is pytest's default, which is wider and, more
    to the point, could change under us on a pytest upgrade."""
    cp = _cfg()
    assert cp.has_section("pytest"), "pytest.ini missing or has no [pytest]"
    assert cp.get("pytest", "python_files").strip() == "test_*.py"


def test_the_glob_does_not_also_match_the_other_conventional_form():
    """pytest's default also matches `*_test.py`. Leaving it out means a file
    cannot become collectable just by being named the other way round."""
    assert "_test.py" not in _cfg().get("pytest", "python_files")


@pytest.mark.parametrize("script", _LIVE_SCRIPTS)
def test_the_live_scripts_are_not_named_like_tests(script):
    """If one of these is ever renamed to `test_*.py`, it gets collected and CI
    posts to real accounts. Fail here instead."""
    path = _TESTS / script
    if not path.exists():
        pytest.skip(f"{script} no longer present")
    assert not script.startswith("test_"), (
        f"{script} would be collected and it posts to live platforms")


def test_a_script_named_like_a_test_yields_no_collectable_tests():
    """The precise invariant: a `test_*.py` file may hold a script entrypoint
    ONLY if pytest collects nothing from it.

    `test_chapter_after_preview_only.py` is the live example — it is a
    SquidgeWorld operator script (creates a draft, posts a chapter, deletes the
    work) that ended up inside the test namespace. It is inert today purely
    because its entrypoint is `main()`, so pytest imports the module and
    collects zero tests. Add one `test_`-prefixed function to it and CI would
    hit SquidgeWorld on every push.

    Two earlier attempts at this test were wrong in instructive ways: the first
    searched for substrings like "asyncio.run(" and flagged ITSELF, because
    those markers appeared in its own source; the second banned main-guards
    outright and flagged `test_posting_helpers.py`, whose `unittest.main()` is
    an ordinary convention that does nothing under pytest. The property that
    actually matters is not "has a main-guard" — it is "is inert when
    collected".
    """
    import ast
    import re
    guard = re.compile(r"^if __name__ == ", re.MULTILINE)
    offenders = []
    for f in sorted(_TESTS.glob("test_*.py")):
        src = f.read_text(encoding="utf-8", errors="replace")
        if not guard.search(src):
            continue
        try:
            tree = ast.parse(src)
        except SyntaxError:                       # pragma: no cover
            continue
        collected = [n.name for n in tree.body
                     if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                     and n.name.startswith("test_")]
        collected += [n.name for n in tree.body
                      if isinstance(n, ast.ClassDef) and n.name.startswith("Test")]
        # unittest.TestCase subclasses are collected too, but they are ordinary
        # tests; the hazard is a SCRIPT that also exposes collectable tests.
        if collected and "unittest" not in src:
            offenders.append((f.name, collected))
    assert offenders == [], (
        "script-style files exposing collectable tests — these would RUN in CI: "
        f"{offenders}")


def test_every_non_test_file_in_tests_is_genuinely_uncollectable():
    """Belt and braces on the glob itself: assert directly that no file in
    tests/ outside the `test_*.py` convention would match it."""
    import fnmatch
    pattern = _cfg().get("pytest", "python_files").strip()
    leaked = [f.name for f in _TESTS.glob("*.py")
              if not f.name.startswith("test_")
              and f.name != "conftest.py"
              and fnmatch.fnmatch(f.name, pattern)]
    assert leaked == [], f"non-test files matching {pattern!r}: {leaked}"
