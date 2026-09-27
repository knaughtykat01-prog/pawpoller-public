"""RESIZERACE (4.40.1): a slow Overview render must not paint over a later page.

Found verifying spec 007: resizing across the mobile breakpoint re-runs route()
on the Overview; that render fetches every platform, and if the user navigated
meanwhile it painted the Overview over the new page. route() now bumps a
generation; the Overview takes a token first and checks it before painting.
"""
from __future__ import annotations


def _app():
    return open("frontend/js/app.js", encoding="utf-8").read()


def test_every_platform_dashboard_checks_its_token_before_every_paint():
    """Same race on the 23 per-platform dashboards (one fetch, then paint)."""
    import re
    app = _app()
    names = re.findall(r"\n    async (render\w*Dashboard)\(\) \{", app)
    assert len(names) >= 23
    for name in names:
        i = app.index(f"    async {name}() {{")
        body = app[i:app.index("\n    },", i)]
        assert body.index("const token = this._routeToken();") < body.index("await "), name
        lines = body.splitlines()
        for k, line in enumerate(lines):
            if line.strip().startswith("this._setContent("):
                assert lines[k - 1].strip().startswith("if (this._stale(token)) return;"), (name, line)


def test_every_route_change_bumps_the_generation():
    app = _app()
    i = app.index("    route() {")
    assert "this._routeGen = (this._routeGen || 0) + 1;" in app[i:i + 400]


def test_the_overview_checks_its_token_before_every_paint():
    app = _app()
    i = app.index("    async renderOverview() {")
    body = app[i:app.index("\n    },", i)]
    assert body.index("const token = this._routeToken();") < body.index("await ")
    # after the last fetch, before building the page; and in the error path
    assert body.count("if (this._stale(token)) return;") == 2
    assert body.index("if (this._stale(token)) return;") < body.index("this._renderDashboard();")
    catch = body[body.index("} catch (err) {"):]
    assert catch.index("this._stale(token)") < catch.index("this._setContent(")


# ── Every page, not just the dashboards (4.40.1, "it'll still load the other page") ──

import re as _re
from pathlib import Path as _Path


def _async_bodies(text, indent_re=r"[ \t]+"):
    for m in _re.finditer(r"\n(" + indent_re + r")async (\w+)\(([^)]*)\) \{", text):
        end = text.find("\n" + m.group(1) + "},", m.end())
        if end != -1:
            yield m.group(2), text[m.end():end]


def test_every_app_page_that_loads_then_paints_is_guarded():
    unguarded = [name for name, body in _async_bodies(_app(), r" {4}")
                 if "await " in body and "this._setContent(" in body
                 and "this._routeToken()" not in body and "_routeToken()" not in body]
    assert unguarded == []


def test_every_module_page_that_loads_then_paints_is_guarded():
    """Library, pieces, stories, posts, inbox, boards… paint #app themselves."""
    bad = []
    for f in _Path("frontend/js").glob("*.js"):
        if f.name in ("app.js", "lazy_vendor.js"):
            continue
        text = f.read_text(encoding="utf-8")
        for name, body in _async_bodies(text):
            paints = ("App._setContent(" in body
                      or _re.search(r"getElementById\('app'\)[\s\S]*\.innerHTML\s*=", body))
            if "await " in body and paints and "App._routeToken()" not in body:
                bad.append(f"{f.name}:{name}")
    assert bad == []


def test_a_detail_page_does_not_adopt_a_stale_items_data():
    """Story A -> B quickly: A's slower fetch set this._data = A under B's address,
    so a save could act on the wrong story. The check comes BEFORE the adopt."""
    for f, adopt in (("story_board.js", "this._data = d;"), ("post_board.js", "this._data = d;")):
        text = _Path("frontend/js", f).read_text(encoding="utf-8")
        i = text.index(adopt)
        assert "if (App._stale(_rt)) return;" in text[i - 200:i], f


def test_clicking_the_page_you_are_on_reloads_it():
    app = _app()
    i = app.index("    navigate(path) {")
    nav = app[i:i + 700]
    assert "=== window.location.hash) { this._rerouteSame(); return; }" in nav
    j = app.index("    _rerouteSame() {")
    rer = app[j:j + 300]
    assert "Editor.isDirty" in rer and "this.route();" in rer, "never drop unsaved editor work"
    k = app.index("window.addEventListener('hashchange', () => this.route());")
    click = app[k:k + 900]
    assert "a.getAttribute('href') !== window.location.hash" in click and "this._rerouteSame();" in click
    assert "e.ctrlKey || e.metaKey || e.shiftKey || e.altKey" in click, "new-tab clicks are the browser's"
