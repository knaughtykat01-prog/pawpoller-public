"""Settings → Preferences → Display timezone (backlog TZPICK).

Two gaps: the menu offered twenty hand-picked cities, so anyone living outside them had to
settle for a neighbouring one, and a fresh install sits on the stored default of UTC — which
reads as plainly wrong on Telegram messages and the digest. `App._timezoneOptions()` now
offers every zone the browser knows, this computer's first, and the wizard saves that zone on
its way out.

The options are built by a pure function, so node evaluates the real `app.js` and calls it;
the wizard line is a source check, the same style as test_hash_names_and_tg_chart.py.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

APP = open("frontend/js/app.js", encoding="utf-8").read()

# Enough of a browser for app.js to define its objects; it touches the DOM only inside methods.
HARNESS = """
const fs = require('fs');
const src = fs.readFileSync('frontend/js/app.js', 'utf8') + ';globalThis.__App = App;';
const stub = {addEventListener(){}, removeEventListener(){}, getElementById(){return null},
              querySelectorAll(){return []}, querySelector(){return null},
              documentElement:{}, body:{}, createElement(){return {style:{}, classList:{add(){}, remove(){}}}}};
global.window = global; global.document = stub;
global.localStorage = {getItem(){return null}, setItem(){}, removeItem(){}};
global.sessionStorage = global.localStorage;
global.location = {hash:'', href:''}; global.navigator = {userAgent:'node'};
global.fetch = () => Promise.reject(new Error('no net'));
(0, eval)(src);
"""


def _options(current: str, tz: str = "") -> list[dict]:
    script = HARNESS + """
    const html = globalThis.__App._timezoneOptions(%s);
    const out = [...html.matchAll(/<option value="([^"]*)"( selected)?>([^<]*)</g)]
        .map(m => ({value: m[1], selected: !!m[2], label: m[3]}));
    console.log(JSON.stringify(out));
    """ % json.dumps(current)
    env = {**os.environ, **({"TZ": tz} if tz else {})}
    r = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=120, env=env)
    assert r.returncode == 0, r.stderr[-600:]
    return json.loads(r.stdout)


class TestTheWizardSetsIt:
    def test_finishing_setup_saves_this_computers_zone(self):
        assert "Intl.DateTimeFormat().resolvedOptions().timeZone" in APP
        assert "display_timezone: tz" in APP

    def test_the_hand_picked_city_list_is_gone(self):
        assert "'Sydney (AEST/AEDT)'" not in APP
        assert "App._timezoneOptions(" in APP


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
class TestTheMenu:
    def test_every_zone_the_browser_knows_is_offered(self):
        opts = self._opts = _options("UTC")
        assert len(opts) > 100, len(opts)
        assert any(o["value"] == "America/Sao_Paulo" for o in opts), "a zone the old list lacked"

    def test_this_computer_comes_first_and_says_so(self):
        first = _options("UTC")[0]
        assert first["label"].startswith("This computer")
        assert "/" in first["value"] or first["value"] == "UTC"

    def test_exactly_one_option_is_selected(self):
        for current in ("UTC", "Europe/London", "America/Denver"):
            selected = [o for o in _options(current) if o["selected"]]
            assert [o["value"] for o in selected] == [current], (current, selected)

    def test_a_zone_the_browser_does_not_know_is_kept(self):
        """A value already saved must never vanish from the menu — it is the live setting."""
        opts = _options("Mars/Olympus")
        assert [o["value"] for o in opts if o["selected"]] == ["Mars/Olympus"]

    def test_no_zone_is_offered_twice(self):
        values = [o["value"] for o in _options("UTC")]
        assert len(values) == len(set(values))


    def test_a_machine_set_to_utc_does_not_get_utc_twice(self):
        """CI runs on UTC, where "this computer" and the plain UTC row are the same zone —
        listing both offered it twice and selected both (caught by the public build)."""
        opts = _options("UTC", tz="UTC")
        values = [o["value"] for o in opts]
        assert values.count("UTC") == 1, values[:4]
        assert [o["value"] for o in opts if o["selected"]] == ["UTC"]
        assert opts[0]["label"].startswith("This computer")

    def test_a_machine_somewhere_else_still_offers_utc(self):
        opts = _options("UTC", tz="America/New_York")
        assert opts[0]["value"] == "America/New_York"
        assert [o["value"] for o in opts if o["selected"]] == ["UTC"]
