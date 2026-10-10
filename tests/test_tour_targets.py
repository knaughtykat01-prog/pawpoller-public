"""Every guided-tour step points at something the app still draws (TOURFRESH).

Tours skip a step whose target is missing, so a renamed id or class makes a step vanish without a sound —
the 2.155.0 and 4.54 redesigns both left steps touring nothing. This reads each target's ids, classes and
data attributes and checks each still appears in the front end's own source.
"""
from __future__ import annotations

import glob
import re

import pytest


def _source() -> str:
    files = [f for f in glob.glob("frontend/js/*.js") if not f.endswith("tour.js")] + ["frontend/index.html"]
    return "".join(open(f, encoding="utf-8").read() for f in files)


def _targets() -> list[str]:
    tour = open("frontend/js/tour.js", encoding="utf-8").read()
    return sorted(set(re.findall(r"target: '([^']+)'", tour)))


SRC = _source()


@pytest.mark.parametrize("selector", _targets())
def test_tour_target_exists(selector):
    parts = re.findall(r'#([\w-]+)|\.([\w-]+)|\[([\w-]+)(?:="([^"]+)")?\]', selector)
    assert parts, selector
    for ident, cls, attr, value in parts:
        if ident:
            assert re.search(rf'id="{re.escape(ident)}"|id=\\?"{re.escape(ident)}|getElementById\(.{re.escape(ident)}.\)',
                             SRC) or f'"{ident}"' in SRC or f"'{ident}'" in SRC, f"{selector}: no #{ident}"
        elif cls:
            assert re.search(rf'class="[^"]*\b{re.escape(cls)}\b|classList\.add\(.{re.escape(cls)}|className = .[^\n]*\b{re.escape(cls)}\b',
                             SRC), f"{selector}: no .{cls}"
        elif value:
            templated = f"{attr}=\"${{" in SRC and (f"'{value}'" in SRC or f'"{value}"' in SRC)
            assert f'{attr}="{value}"' in SRC or templated, f"{selector}: no [{attr}={value}]"
        else:
            assert attr in SRC, f"{selector}: no [{attr}]"


def test_no_step_still_describes_the_old_pages():
    tour = open("frontend/js/tour.js", encoding="utf-8").read()
    for stale in ("text-only</em> for now", "health dot", "Stories hub", "Run <em>pawsync", "four sections down this rail"):
        assert stale not in tour, stale
