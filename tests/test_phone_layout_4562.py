"""4.56.2 — three phone fixes from the operator's screenshots.

PHONEBAR: the ☰ / 18+ / bell / avatar buttons floated over the page, so content scrolled under
them; a solid strip now sits behind them and the page starts below it. LIBROW: the Library's
one sideways-swipe row hid search and every filter off the right edge. TZPICK: the time zone
menu was ~420 raw names in one run; now grouped, with UTC offsets.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

CSS = {n: Path(f"frontend/css/{n}.css").read_text(encoding="utf-8") for n in ("layout", "bookshelf")}


def test_a_solid_bar_sits_behind_the_phone_buttons():
    css = CSS["layout"]
    # PHONEBAR2 (4.58.0): a real element — iOS Safari ignores a pseudo-element when it fills
    # the status-bar area, so the page showed through above the bar.
    assert 'html[data-mobile="1"] .mobile-bar-bg' in css and 'html[data-mobile="1"] body::before' not in css
    assert 'id="mobile-bar-bg"' in Path("frontend/index.html").read_text(encoding="utf-8")
    assert 'html[data-mobile="1"] .main-col { padding-top: var(--mbar-h); }' in css
    # The page no longer pads around floating buttons.
    assert ".page-header { padding-right: 150px; }" not in css


def test_the_library_filters_wrap_on_a_phone_instead_of_hiding_off_screen():
    css = CSS["bookshelf"]
    phone = css[css.index("LIBROW"):]
    assert "display: grid; grid-template-columns: 1fr 1fr" in phone
    assert "#shelf-controls #shelf-search { grid-column: 1 / -1; }" in phone
    assert "flex-wrap: nowrap; overflow-x: auto" not in phone.split(".shelf-segs")[0]


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_time_zone_menu_is_grouped_with_offsets():
    from tests.test_display_timezone import HARNESS
    script = HARNESS + """
    const html = globalThis.__App._timezoneOptions('Europe/London');
    const groups = [...html.matchAll(/<optgroup label="([^"]*)"/g)].map(m => m[1]);
    const opts = [...html.matchAll(/<option value="([^"]*)"( selected)?>([^<]*)</g)];
    console.log(JSON.stringify({groups, selected: opts.filter(m => m[2]).map(m => m[1]),
        values: opts.map(m => m[1]), london: (opts.find(m => m[1] === 'Europe/London') || [])[3]}));
    """
    r = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=120,
                       env={**__import__("os").environ, "TZ": "Australia/Sydney"})
    assert r.returncode == 0, r.stderr[-600:]
    out = json.loads(r.stdout)
    assert out["groups"][0] == "Common" and "Asia" in out["groups"] and "Europe" in out["groups"]
    assert out["selected"] == ["Europe/London"]                     # one, and the right one
    assert len(out["values"]) == len(set(out["values"])), "a zone listed twice selects twice"
    assert "UTC" in out["london"]                                    # "Europe/London · UTC+01:00"


def test_the_e621_tag_backups_are_classified():
    """The server's Settings → Privacy listed five data/e621_tag_backup_<unix>.json files as
    unclassified (written by the one-off E6CAT retag, 2026-10-01)."""
    import datamap
    for name in ("data/e621_tag_backup_1790833248.json", "data/e621_tag_backup_1790834097.json"):
        assert datamap.path_class(name) == "confidential", name
    assert datamap.path_class("data/e621_tag_backup_notatime.json") is None
