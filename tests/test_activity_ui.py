"""Activity & progress, the page side (spec 017): the ring is gone, pages paint skeletons,
publish screens go through the activity job, and the pill's module is loaded."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
JS = ROOT / "frontend" / "js"


def _read(name):
    return (JS / name).read_text(encoding="utf-8")


def test_the_fetch_count_ring_is_gone():
    src = _read("loading_indicator.js")
    assert "patchedFetch" not in src and "pp-spinner-host" not in src
    assert "window.toast" in src          # the toasts stay


def test_the_five_pages_paint_a_skeleton_not_a_loading_line():
    app = _read("app.js")
    for fn, kind in (("async renderOverview() {", "overview"), ("async renderPlatformsHub() {", "rows"),
                     ("async renderAnalytics() {", "chart")):
        body = app[app.index(fn):app.index(fn) + 400]
        assert f"this._loading('{kind}'" in body, fn
    assert "Utils.skeleton('cards'" in _read("bookshelf.js")
    assert "Utils.skeleton('rows'" in _read("posts.js")
    for name in ("bookshelf.js", "posts.js"):
        first = _read(name)
        assert not re.search(r'id="(shelf-grid|post-feed)"><div class="loading-spinner">', first), name
    assert '_loading(kind = \'rows\'' in app and "loading-spinner\">Loading...</div>" not in app


@pytest.mark.skipif(shutil.which("node") is None, reason="node is needed")
def test_a_skeleton_names_what_it_waits_for():
    script = ("const fs=require('fs');eval(fs.readFileSync(" + json.dumps(str(JS / "utils.js"))
              + ",'utf8')+';globalThis.Utils=Utils;');"
              "console.log(JSON.stringify([Utils.skeleton('cards',3,'the <Library>'),Utils.skeleton()]));")
    r = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=60, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    cards, rows = json.loads(r.stdout)
    assert cards.count('class="skel-card"') == 3 and "Still loading the &lt;Library&gt;" in cards
    assert 'aria-busy="true"' in rows and "skel-slow" in rows


def test_publish_screens_run_as_an_activity_job():
    for name in ("artwork.js", "masterpieces.js", "posts.js", "publish_check.js"):
        src = _read(name)
        assert "Activity.run(" in src, name
    # a minimised / navigated-away run resolves null and the screen stops
    for name in ("artwork.js", "masterpieces.js", "posts.js"):
        assert "if (!res) return;" in _read(name), name


def test_the_pill_module_is_loaded_and_uses_the_one_read():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    assert '<script src="/js/activity.js' in html and 'class="route-hair"' in html
    src = _read("activity.js")
    assert "fetch('/api/activity'" in src and "visibilitychange" in src
    assert "role', 'dialog'" in src and "role=\"progressbar\"" in src and "Escape" in src
