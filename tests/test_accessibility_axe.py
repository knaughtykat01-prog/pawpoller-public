"""Automated accessibility scan, WCAG 2.2 AA (spec 012): every marketing-site page and the app's key screens.

axe-core (a rule engine — no AI) runs in headless Chromium. Any violation fails, naming the page, the rule
and the element. Tools catch roughly a third of WCAG; the rest is checked by hand (see the spec's
quickstart) — this keeps the automatable third from slipping back.

Dev-only tooling: axe-core is a devDependency of the marketing site and Chromium comes with Playwright.
Neither ships with the app. Without them (the public CI, the release image) these tests skip, with the reason.
"""
from __future__ import annotations

import functools
import http.server
import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

import config

ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / "site"
DIST = SITE / "dist"
AXE = SITE / "node_modules" / "axe-core" / "axe.min.js"
TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"]


def _tooling_missing() -> str:
    if not AXE.exists():
        return "axe-core is not installed (npm install in site/)"
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except ImportError:
        return "Playwright for Python is not installed"
    return ""


pytestmark = [pytest.mark.skipif(bool(_tooling_missing()), reason=_tooling_missing() or "ok"),
              pytest.mark.repo_only]


@pytest.fixture(scope="module")
def browser():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as e:  # noqa: BLE001
            pytest.skip(f"Chromium is not available: {e}")
        yield b
        b.close()


def _scan(browser, url: str, theme: str | None = None, before=None) -> list[str]:
    ctx = browser.new_context(bypass_csp=True, viewport={"width": 1280, "height": 900})
    page = ctx.new_page()
    try:
        page.goto(url, wait_until="networkidle")
        if theme:
            page.evaluate("t => document.documentElement.setAttribute('data-theme', t)", theme)
        if before:
            before(page)
        page.wait_for_timeout(300)
        page.add_script_tag(content=AXE.read_text(encoding="utf-8"))
        res = page.evaluate("tags => axe.run(document, {runOnly: {type: 'tag', values: tags}})", TAGS)
    finally:
        ctx.close()
    where = url + (f" [{theme}]" if theme else "")

    def detail(n):
        d = next((c.get("data") for c in n.get("any", []) if isinstance(c.get("data"), dict)), None)
        if d and "contrastRatio" in d:          # say which colours, so the fix is obvious
            return f" ({d.get('fgColor')} on {d.get('bgColor')} = {d.get('contrastRatio')}, needs {d.get('expectedContrastRatio')})"
        return ""
    return [f"{where}: {v['id']} — {v['help']} — {n['target']}{detail(n)}"
            for v in res["violations"] for n in v["nodes"]]


# ── Marketing site ───────────────────────────────────────────────────────────

def _site_is_stale() -> bool:
    built = DIST / "index.html"
    if not built.exists():
        return True
    newest = max(p.stat().st_mtime for d in (SITE / "src", SITE / "public") for p in d.rglob("*") if p.is_file())
    newest = max(newest, (SITE / "tailwind.config.mjs").stat().st_mtime)
    return newest > built.stat().st_mtime


@pytest.fixture(scope="module")
def site_url():
    if _site_is_stale():
        npm = shutil.which("npm") or shutil.which("npm.cmd")
        if not npm:
            pytest.skip("the site's build is stale and npm isn't available to rebuild it")
        subprocess.run([npm, "run", "build"], cwd=SITE, check=True, capture_output=True, timeout=300)
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(DIST))
    handler.log_message = lambda *a, **k: None
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def _site_pages() -> list[str]:
    if not DIST.exists():
        return ["/"]
    pages = sorted("/" + p.parent.relative_to(DIST).as_posix().strip(".") for p in DIST.rglob("index.html"))
    return [p if p.endswith("/") else p + "/" for p in pages] + ["/404.html"]


def test_every_site_page_passes_axe(browser, site_url):
    fails = []
    for path in _site_pages():
        fails += _scan(browser, site_url + path)
    assert not fails, "\n".join(fails)


# ── The app's key screens ────────────────────────────────────────────────────

@pytest.fixture
def app_url():
    import uvicorn

    import dashboard
    config.save_settings({"setup_complete": True, "setup_mode": "server", "tours_seen": ["*"]})
    from database.db import get_connection
    conn = get_connection()
    try:
        for i, title in enumerate(("Sample Story", "Inkwolf Sketch", "Penwright Tale"), 1):
            conn.execute("INSERT INTO submissions (submission_id, title, username, create_datetime, views, "
                         "favorites_count, comments_count) VALUES (?, ?, 'SecondFur', '2026-09-01', ?, ?, ?)",
                         (1000 + i, title, 10 * i, i, 0))
        conn.commit()
    finally:
        conn.close()
    server = uvicorn.Server(uvicorn.Config(dashboard.app, host="127.0.0.1", port=0, log_level="warning",
                                           lifespan="off"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    deadline = time.time() + 15
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    port = server.servers[0].sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    t.join(timeout=10)


APP_SCREENS = ["/#/", "/#/library", "/#/accounts", "/#/posts", "/#/settings", "/#/settings/privacy"]


@pytest.mark.parametrize("theme", ["default", "light"])
def test_the_app_key_screens_pass_axe(browser, app_url, theme):
    fails = []
    for screen in APP_SCREENS:
        fails += _scan(browser, app_url + screen, theme=None if theme == "default" else theme)
    assert not fails, "\n".join(fails)


# ── What axe can't judge, measured instead (spec 012 hand checks, kept as tests) ──

def test_every_site_page_reflows_at_320px(browser, site_url):
    """1.4.10: no sideways scrolling on a 320 px screen (code blocks may scroll inside themselves)."""
    fails = []
    ctx = browser.new_context(viewport={"width": 320, "height": 800})
    page = ctx.new_page()
    try:
        for path in _site_pages():
            page.goto(site_url + path, wait_until="networkidle")
            over = page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
            if over > 1:
                fails.append(f"{path}: {over}px wider than the screen")
    finally:
        ctx.close()
    assert not fails, "\n".join(fails)


def test_site_keyboard_basics(browser, site_url):
    """2.4.1 skip link first, 2.4.7 focus visible, 2.4.11 not hidden under the sticky header."""
    ctx = browser.new_context(viewport={"width": 1280, "height": 800})
    page = ctx.new_page()
    try:
        for path in _site_pages():
            page.goto(site_url + path, wait_until="networkidle")
            page.keyboard.press("Tab")
            first = page.evaluate("[document.activeElement.textContent.trim(), document.activeElement.getAttribute('href')]")
            assert first == ["Skip to content", "#content"], f"{path}: first Tab stop is {first}"
            top = page.evaluate("document.activeElement.getBoundingClientRect().top")
            assert top >= 0, f"{path}: the skip link doesn't come on screen when focused"
            for _ in range(6):
                page.keyboard.press("Tab")
                ring = page.evaluate("""() => { const s = getComputedStyle(document.activeElement);
                    return s.outlineStyle !== 'none' && parseFloat(s.outlineWidth) >= 2; }""")
                assert ring, f"{path}: no visible focus ring on {page.evaluate('document.activeElement.outerHTML.slice(0,80)')}"
    finally:
        ctx.close()


def test_reduced_motion_stops_transitions(browser, site_url, app_url):
    """2.3.3: with reduced motion asked for, nothing animates — site and app."""
    ctx = browser.new_context(reduced_motion="reduce")
    page = ctx.new_page()
    try:
        for url, sel in ((site_url + "/", ".btn-primary"), (app_url + "/#/", ".btn, button")):
            page.goto(url, wait_until="networkidle")
            dur = page.evaluate(f"getComputedStyle(document.querySelector('{sel}')).transitionDuration")
            assert all(float(d.rstrip("ms").rstrip("s") or 0) < 0.01 or d.endswith("ms") and float(d[:-2]) < 1
                       for d in dur.split(",")), f"{url}: transitions still run ({dur})"
    finally:
        ctx.close()


def test_the_app_key_screens_work_at_200_percent_zoom(browser, app_url):
    """1.4.4: at 200% zoom (a 640 px-wide viewport at 2x) no key screen needs sideways scrolling."""
    fails = []
    ctx = browser.new_context(viewport={"width": 640, "height": 450}, device_scale_factor=2)
    page = ctx.new_page()
    try:
        for screen in APP_SCREENS:
            page.goto(app_url + screen, wait_until="networkidle")
            page.wait_for_timeout(300)
            over = page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
            if over > 1:
                fails.append(f"{screen}: {over}px wider than the screen")
    finally:
        ctx.close()
    assert not fails, "\n".join(fails)


def test_the_library_filter_bar_floats(browser, app_url):
    """4.45.1: the Library's filters stay on screen while the shelf scrolls — desktop and phone.
    On phones it once scrolled away because the main area's `overflow-x: hidden` made it a scroll
    container (sticky then sticks to it, not the window); `overflow-x: clip` doesn't."""
    from database.db import get_connection
    conn = get_connection()
    try:
        for i in range(60):
            conn.execute("INSERT INTO submissions (submission_id, title, username, create_datetime, views, "
                         "favorites_count, comments_count) VALUES (?, ?, 'SecondFur', '2026-09-01', 1, 1, 0)",
                         (5000 + i, f"Sample Story {i}"))
        conn.commit()
    finally:
        conn.close()
    for vp, expect_top in (({"width": 1400, "height": 800}, 0), ({"width": 390, "height": 800}, 50)):
        page = browser.new_page(viewport=vp)
        try:
            page.goto(app_url + "/#/library", wait_until="networkidle")
            page.wait_for_timeout(500)
            page.mouse.wheel(0, 1500)
            page.wait_for_timeout(400)
            assert page.evaluate("scrollY") > 500, "the shelf didn't scroll — not enough works to test with"
            top = page.evaluate("document.getElementById('shelf-controls').getBoundingClientRect().top")
            assert abs(top - expect_top) <= 2, f"{vp['width']}px wide: the filter bar scrolled away (top={top})"
        finally:
            page.close()
