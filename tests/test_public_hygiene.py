"""Spec 007 (public hygiene), 4.40.0 — the checklist-audit findings.

Site: a real 404, a page list + crawler rules, each page's own canonical/share
address, a headline that names art, the current public version. App: one h1
per page, real links on the Overview figures, text alternatives on every image,
a tab title per page, and the story editor's tools loaded only when it opens.

The site half lives in test_site_hygiene.py (site/ is not in the public
copy). Contract style as test_board_polish.py: the wiring is pinned by source string,
the loader is exercised through node.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest


def _src(path):
    return open(path, encoding="utf-8").read()


def _version(v):
    return tuple(int(x) for x in v.split("."))


# ── App: keyboard and screen reader ──────────────────────────────────────

class TestOneHeadingPerPage:

    def test_the_sidebar_brand_is_not_a_heading(self):
        """It was the <h1> on every page, so each page's own title was a second one."""
        html = _src("frontend/index.html")
        side = html[html.index('class="sidebar-header"'):html.index('class="sidebar-collapse"')]
        assert "<h1" not in side and 'class="brand-name"' in side
        for css in Path("frontend/css").glob("*.css"):
            assert ".sidebar-header h1" not in css.read_text(encoding="utf-8"), css.name

    def test_page_titles_are_h1(self):
        stale = re.compile(r'class="page-header[^"]*"[^>]*>\s*<h2\b')
        for js in Path("frontend/js").glob("*.js"):
            assert not stale.search(js.read_text(encoding="utf-8")), f"{js.name}: page title still an h2"

    def test_every_page_header_h2_rule_has_an_h1_twin(self):
        """Promoting the tag must not restyle 111 page titles."""
        for css in Path("frontend/css").glob("*.css"):
            for line in css.read_text(encoding="utf-8").splitlines():
                if ".page-header h2" in line:
                    assert ".page-header h1" in line, f"{css.name}: {line.strip()}"


class TestLinksImagesTitles:

    def test_overview_figures_are_real_links(self):
        app = _src("frontend/js/app.js")
        tiles = re.findall(r'<a class="(?:dash-stat-link|dash-ql)"[^>]*>', app)
        assert len(tiles) == 5
        assert all("href=" in a for a in tiles), tiles

    def test_a_modified_click_is_left_to_the_browser(self):
        """Ctrl/Cmd/Shift-click opens a new tab; it must not ALSO navigate this one."""
        app = _src("frontend/js/app.js")
        i = app.index("if (d.nav !== undefined) {")
        block = app[i:i + 700]
        assert "e.ctrlKey || e.metaKey || e.shiftKey" in block and "e.preventDefault()" in block

    def test_every_image_template_has_a_text_alternative(self):
        pat = re.compile(r"<img\b(?![^>]*\balt=)[^>]*\b(?:src|class|id)=[^>]*>")
        files = list(Path("frontend/js").glob("*.js")) + [Path("frontend/index.html")]
        bad = [f"{f.name}: {m.group(0)[:70]}" for f in files
               for m in pat.finditer(f.read_text(encoding="utf-8"))]
        assert bad == []

    def test_the_tab_title_follows_the_page_heading(self):
        app = _src("frontend/js/app.js")
        assert "this._watchTitle();" in app
        i = app.index("_watchTitle() {")
        body = app[i:i + 1400]
        assert "querySelector('h1')" in body and "· PawPoller" in body and "MutationObserver" in body

    def test_the_story_editor_names_its_story_in_a_heading(self):
        """The toolbar title was a <span>: the editor had no h1, so its tab read
        just "PawPoller" — found on the demo sweep."""
        assert '<h1 class="editor-title" id="editor-title">' in _src("frontend/js/editor.js")

    def test_a_badge_inside_the_heading_stays_out_of_the_tab_title(self):
        app = _src("frontend/js/app.js")
        i = app.index("_watchTitle() {")
        assert "n.nodeType === 3" in app[i:i + 1400]

    def test_the_not_found_view_has_a_heading_too(self):
        assert "<h1>Page not found</h1>" in _src("frontend/js/app.js")


# ── App: the editor's tools load when the editor opens ───────────────────

_FAKE_DOM = r"""
const added = []; let failOn = null;
globalThis.location = { href: 'http://x/' };
globalThis.document = {
  currentScript: { src: 'http://x/js/lazy_vendor.js?v=9.9.9' },
  createElement: () => ({ remove() {} }),
  head: { appendChild(s) { added.push(s.src);
    setTimeout(() => (failOn && s.src.includes(failOn)) ? s.onerror() : s.onload(), 0); } },
};
globalThis.window = globalThis;
"""


def _node(script):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    src = _src("frontend/js/lazy_vendor.js")
    out = subprocess.run([node, "-e", _FAKE_DOM + src + script],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)


class TestLazyVendor:

    def test_loads_the_group_in_order_with_the_page_version(self):
        r = _node("""
          const a = LazyVendor.load('editor'), b = LazyVendor.load('editor');
          Promise.all([a, b]).then(() => LazyVendor.load('editor')).then(() =>
            console.log(JSON.stringify({ same: a === b, added })));""")
        assert r["same"] is True
        assert r["added"] == [f"/js/vendor/{f}?v=9.9.9" for f in (
            "codemirror-bundle.min.js", "turndown.min.js", "beautify.min.js",
            "beautify-html.min.js", "beautify-css.min.js")], "once, in order, versioned"

    def test_a_failure_rejects_and_the_next_call_retries(self):
        r = _node("""
          failOn = 'beautify.min.js';
          LazyVendor.load('editor').then(() => console.log('{"bad":1}'), (e1) => {
            const firstTry = added.length; failOn = null;
            LazyVendor.load('editor').then(() => console.log(JSON.stringify(
              { msg: e1.message, firstTry, total: added.length })));
          });""")
        assert "beautify.min.js" in r["msg"]
        assert r["firstTry"] == 3, "stops at the failed file"
        assert r["total"] == 8, "Retry fetches the whole group again"


class TestEditorWiring:

    def test_no_page_loads_the_editor_tools_up_front(self):
        html = _src("frontend/index.html")
        for f in ("codemirror-bundle", "turndown.min", "beautify"):
            assert f"/js/vendor/{f}" not in html, f
        assert html.index("/js/lazy_vendor.js") < html.index("/js/editor.js")

    def test_the_editor_waits_for_them_and_can_retry(self):
        ed = _src("frontend/js/editor.js")
        i = ed.index("async renderEditor(storyName) {")
        assert ed.index("await LazyVendor.load('editor')", i) < ed.index("this._initCodeMirror(", i)
        body = ed[i:i + 1800]
        assert "editor-load-retry" in body and "couldn't load" in body
        assert body.count("stillHere()") == 3, (
            "navigating away during the first load must not paint the editor over the new page")

    def test_only_the_editor_uses_them(self):
        """If another page starts using one, it must load the group too."""
        pat = re.compile(r"\bCM\.|js_beautify|html_beautify|css_beautify|TurndownService")
        users = {js.name for js in Path("frontend/js").glob("*.js")
                 if js.name not in ("editor.js", "lazy_vendor.js")
                 and pat.search(js.read_text(encoding="utf-8"))}
        assert users == set()
