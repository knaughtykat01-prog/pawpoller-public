"""The app's pages, RUN in a browser — not their source searched for text.

Test audit 2026-09-29, ranked next step 1: only ~25 tests executed any JavaScript, while ~150 "tested" the
frontend by finding text in it. That is how the `window.Utils` / `window.Components` bug hid for months
(fixed 4.43.1): half a dozen features were dead code and every text test still passed, because the text was
there.

These tests boot the real app (`dashboard.app`) on a scratch database filled with a believable install —
a persona, accounts on several sites, works with stats and history on every platform, posts (published,
failed, scheduled), a queue, a commission, an artist — open every page in headless Chromium, and fail on:

- an uncaught exception in the page,
- a `console.error`,
- an `/api/` answer of 500 or worse (a 4xx can be a deliberate "not connected" answer, so it isn't counted),
- a page that renders nothing.

Each failure names the page. Requests to anything but the local app are blocked, so the run never touches
the network. Dev-only tooling: Playwright for Python + Chromium; without them (public CI, the release
image) the tests skip, with the reason.
"""
from __future__ import annotations

import re
import socket
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import config


def _tooling_missing() -> str:
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except ImportError:
        return "Playwright for Python is not installed"
    return ""


ROOT = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.skipif(bool(_tooling_missing()), reason=_tooling_missing() or "ok")


# Every page the side rail and the top bar reach, plus the settings sub-pages people land on.
PAGES = [
    "#/", "#/library", "#/posts", "#/posts/new", "#/platforms", "#/accounts", "#/analytics",
    "#/artists", "#/artwork/new", "#/artwork/quick", "#/boards", "#/characters", "#/collections",
    "#/commissions", "#/editor", "#/getting-started", "#/groups", "#/imagetool", "#/inbox",
    "#/laurels", "#/ledger", "#/posting/log", "#/posting/queue", "#/promo", "#/repost-radar",
    "#/settings", "#/settings/privacy",
]
# The platforms the seed fills — each gets its dashboard, submissions list and (where one exists) compare page.
SEEDED = ["ib", "fa", "da", "ws", "bsky", "e621", "sf", "ao3"]
PLATFORM_PAGES = [f"#/{c}" for c in SEEDED] + [f"#/{c}/submissions" for c in SEEDED]


# ── A believable install ──────────────────────────────────────────────────────

_METRIC = re.compile(r"(count|likes|reposts|replies|quotes|kudos|hits|score|bookmarks|plays|downloads)$")


def _iso(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")


def _fill(conn: sqlite3.Connection, table: str, values: dict) -> None:
    """Insert one row, giving every NOT NULL column without a default a plausible value.

    Driven by the live schema, so a new column doesn't break the seed — it just gets a blank."""
    cols = conn.execute(f"PRAGMA table_info({table})").fetchall()
    row = {}
    for c in cols:
        name, ctype, notnull, default, pk = c[1], (c[2] or "").upper(), c[3], c[4], c[5]
        if name in values:
            row[name] = values[name]
        elif notnull and default is None and not (pk and "INT" in ctype):
            row[name] = 0 if "INT" in ctype or "REAL" in ctype else ""
    names = ", ".join(row)
    conn.execute(f"INSERT INTO {table} ({names}) VALUES ({', '.join('?' * len(row))})", list(row.values()))


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {c[1] for c in conn.execute(f"PRAGMA table_info({table})")}


def _seed(conn: sqlite3.Connection) -> None:
    from database import accounts, personas, posts_queries, posting_queries, commissions_queries, artist_queries

    pid = personas.create_persona(conn, "Inkwolf", "#c47a3a")
    acct = {}
    for code in SEEDED:
        acct[code] = accounts.create_account(conn, code, "", handle="inkwolf", is_default=True)
        if "persona_id" in _columns(conn, "accounts"):
            conn.execute("UPDATE accounts SET persona_id = ? WHERE account_id = ?", (pid, acct[code]))

    titles = ["Moonlit Fox", "Harbour Sketch", "The Lantern Keeper", "Copper Dragon", "Rainy Window",
              "Penwright Tale", "Autumn Study", "Night Market"]
    for code in SEEDED:
        subs = "submissions" if code == "ib" else f"{code}_submissions"
        snaps = "snapshots" if code == "ib" else f"{code}_snapshots"
        scols, ncols = _columns(conn, subs), _columns(conn, snaps)
        for i, title in enumerate(titles):
            sid = 1000 + i
            views, faves, comments = 40 * (i + 1), 5 * (i + 1), i
            row = {"submission_id": sid, "title": title, "username": "inkwolf", "views": views,
                   "favorites_count": faves, "comments_count": comments, "description": f"{title}, a sample."}
            for k in ("create_datetime", "posted_at", "published_at", "created_at", "upload_date"):
                if k in scols:
                    row[k] = _iso(30 - 3 * i)
            if "account_id" in scols:
                row["account_id"] = acct[code]
            for k in scols - row.keys():                 # each site's own numbers: likes, kudos, score…
                if _METRIC.search(k):
                    row[k] = faves
            if "keywords" in scols:
                row["keywords"] = '["fox", "sketch"]'
            _fill(conn, subs, {k: v for k, v in row.items() if k in scols})
            for day in range(5):                         # history, so charts and "trending" have a line
                snap = {"submission_id": sid, "polled_at": _iso(5 - day), "views": views * (day + 1) // 5,
                        "favorites_count": faves * (day + 1) // 5, "comments_count": comments}
                if "account_id" in ncols:
                    snap["account_id"] = acct[code]
                _fill(conn, snaps, {k: v for k, v in snap.items() if k in ncols})

    # Posts: one published to two sites, one that failed, one scheduled, one draft.
    now = _iso(0)
    p1 = posts_queries.create_post(conn, body="New piece up: Moonlit Fox 🦊 #art", now=_iso(2))
    posts_queries.upsert_post_publication(conn, post_id=p1, platform="bsky", account_id=acct["bsky"],
                                          status="posted", external_id="at://x/1",
                                          external_url="https://bsky.app/profile/inkwolf/post/1", now=_iso(2))
    posts_queries.upsert_post_publication(conn, post_id=p1, platform="fa", account_id=acct["fa"],
                                          status="posted", external_id="55", now=_iso(2))
    p2 = posts_queries.create_post(conn, body="Commissions open this weekend!", now=_iso(1))
    posts_queries.upsert_post_publication(conn, post_id=p2, platform="bsky", account_id=acct["bsky"],
                                          status="failed", error="Rate limited", now=_iso(1))
    p3 = posts_queries.create_post(conn, body="Stream tonight at 8", now=now)
    if "scheduled_at" in _columns(conn, "posts"):
        conn.execute("UPDATE posts SET scheduled_at = ? WHERE post_id = ?",
                     ((datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S"), p3))
    posts_queries.create_post(conn, body="A draft I haven't finished", now=now)

    posting_queries.add_to_queue(conn, "Penwright Tale", 1, "ao3", account_id=acct["ao3"],
                                 scheduled_at=(datetime.now(timezone.utc) + timedelta(days=2)).isoformat(),
                                 persona_id=pid)
    commissions_queries.create_commission(conn, client_name="Sample Client", description="Ref sheet",
                                          price=80, status="in_progress" if "in_progress" in
                                          getattr(commissions_queries, "STATUSES", ()) else "quote")
    artist_queries.upsert_artist(conn, "Penwright", handles={"fa": "penwright", "bsky": "penwright.bsky.social"})
    conn.commit()


# ── The app and the browser ────────────────────────────────────────────────────

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


_DUMMY_LOGINS = {
    "username": "inkwolf", "password": "x", "fa_username": "inkwolf", "fa_cookie_a": "x", "fa_cookie_b": "x",
    "ws_api_key": "x", "sf_api_token": "x", "ao3_username": "inkwolf", "ao3_password": "x",
    "da_target_user": "inkwolf", "da_client_id": "1", "da_client_secret": "x",
    "bsky_identifier": "inkwolf.bsky.social", "bsky_app_password": "x", "e621_username": "inkwolf",
    "e621_api_key": "x",
}


def _signed_up() -> dict:
    """An account, its email and the current Terms (4.67.0, spec 033) — or every page is the sign-up."""
    import legal
    cur = legal.current()
    return {"auth_username": "inkwolf", "auth_password_hash": config.hash_password("sample-password"),
            "auth_email": "owner@example.com", "tech_reports": False, "tech_usage": False,
            "legal_accepted": {"terms": cur["terms"], "privacy": cur["privacy"], "at": "2026-10-11T00:00:00+00:00"}}


_SIGNED_UP = _signed_up()


@pytest.fixture
def _offline(monkeypatch):
    """The app may not reach the network while a page is open — the browser is fenced in by _Watched,
    the server by this. A refused connection is what an offline install sees, so pages must cope."""
    real = socket.socket.connect

    def connect(self, address):
        host = address[0] if isinstance(address, tuple) else ""
        if host and host not in ("127.0.0.1", "::1", "localhost"):
            raise ConnectionRefusedError(f"test_frontend_runs: no network ({host})")
        return real(self, address)
    monkeypatch.setattr(socket.socket, "connect", connect)


@pytest.fixture
def app_url(_offline):
    import uvicorn

    import dashboard
    from database.db import get_connection
    # Every guided tour marked seen (names read from tour.js, so a new tour is covered), or the welcome
    # tour sits over every page. Dummy logins for the seeded sites, so their dashboards draw their
    # charts instead of "not connected" — nothing can use them: see _offline.
    tours = re.findall(r"^ {8}'([a-z0-9-]+)': \[", (ROOT / "frontend/js/tour.js").read_text(encoding="utf-8"), re.M)
    config.save_settings({"setup_complete": True, "age_band": "adult", "setup_mode": "server", "tours_seen": tours,
                          "display_timezone": "Australia/Sydney", **_DUMMY_LOGINS, **_SIGNED_UP})
    config.invalidate_auth_required_cache()
    conn = get_connection()
    try:
        _seed(conn)
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
    # The account would make every later test's TestClient need a login.
    config.delete_settings_keys(list(_SIGNED_UP))
    config.invalidate_auth_required_cache()


class _Watched:
    """One browser tab on the app that records everything that went wrong."""

    def __init__(self, browser, base: str, viewport: dict):
        self.base = base
        self.ctx = browser.new_context(viewport=viewport)
        # Nothing leaves the machine: fonts, avatars, platform thumbnails are all refused.
        self.ctx.route("**/*", lambda r: r.continue_() if r.request.url.startswith(base) else r.abort())
        # Signed in (4.67.0): every install has an account now.
        self.ctx.add_cookies([{"name": "pp_session", "value": config.sign_session({"u": "inkwolf", "r": True}),
                               "url": base}])
        self.page = self.ctx.new_page()
        self.errors: list[str] = []
        self.page.on("pageerror", lambda e: self.errors.append(f"uncaught: {e}"))
        self.page.on("console", self._console)
        self.page.on("response", self._response)
        self.page.goto(base + "/#/", wait_until="networkidle")

    def _console(self, msg):
        # Refused off-site requests log as failed loads; they are ours, not the app's.
        if msg.type == "error" and "ERR_FAILED" not in msg.text and "net::" not in msg.text:
            self.errors.append(f"console.error: {msg.text[:300]}")

    def _response(self, r):
        if r.status >= 500 and "/api/" in r.url:
            self.errors.append(f"HTTP {r.status} {r.request.method} {r.url.replace(self.base, '')}")

    def open(self, hash_: str) -> list[str]:
        self.errors.clear()
        self.page.evaluate("h => { location.hash = h; }", hash_)
        self.page.wait_for_load_state("networkidle")
        self.page.wait_for_timeout(400)
        found = list(dict.fromkeys(self.errors))
        text = self.page.evaluate("(document.querySelector('#main-content, .main-content, main') || document.body)"
                                  ".innerText.trim().length")
        if text < 20:
            found.append(f"the page rendered nothing ({text} characters)")
        return [f"{hash_}: {e}" for e in found]

    def close(self):
        self.ctx.close()


def _visit_all(browser, url, viewport, pages) -> list[str]:
    tab = _Watched(browser, url, viewport)
    try:
        fails = list(dict.fromkeys(tab.errors))           # anything the first load threw
        for h in pages:
            fails += tab.open(h)
    finally:
        tab.close()
    return fails


# ── The tests ──────────────────────────────────────────────────────────────────

def test_every_page_runs_without_errors(browser, app_url):
    fails = _visit_all(browser, app_url, {"width": 1400, "height": 900}, PAGES + PLATFORM_PAGES)
    assert not fails, "\n".join(fails)


def test_every_page_runs_without_errors_on_a_phone(browser, app_url):
    """The phone layout runs different code: the bottom-sheet menus, the moved top-bar controls (4.53.0)."""
    fails = _visit_all(browser, app_url, {"width": 390, "height": 844}, PAGES)
    assert not fails, "\n".join(fails)


def test_the_seeded_data_reaches_the_screen(browser, app_url):
    """Guards the guard: if the seed stopped landing, every page would be 'clean' and prove nothing."""
    tab = _Watched(browser, app_url, {"width": 1400, "height": 900})
    try:
        for hash_, expect in (("#/fa/submissions", "Moonlit Fox"), ("#/ib/submissions", "Night Market"),
                              ("#/posts", "Commissions open this weekend"), ("#/commissions", "Sample Client")):
            tab.open(hash_)
            tab.page.wait_for_function("t => document.body.innerText.includes(t)", arg=expect, timeout=5000)
    finally:
        tab.close()


def test_a_broken_page_is_caught(browser, app_url):
    """Guards the guard: an exception thrown while a page renders must fail the run."""
    tab = _Watched(browser, app_url, {"width": 1400, "height": 900})
    try:
        tab.page.evaluate("setTimeout(() => { throw new Error('planted by the test'); }, 0)")
        tab.page.wait_for_timeout(100)
        assert any("planted by the test" in e for e in tab.errors), tab.errors
    finally:
        tab.close()


# ── The newest screens, clicked through (specs 018–020) ───────────────────────────

def test_the_account_menu_opens_closes_and_navigates(browser, app_url):
    """Spec 019: the avatar menu opens, Escape closes it and hands focus back, and its items go somewhere."""
    tab = _Watched(browser, app_url, {"width": 1400, "height": 900})
    page = tab.page
    try:
        btn, menu = page.locator("#account-menu-btn"), page.locator("#account-menu")
        btn.click()
        assert menu.is_visible() and btn.get_attribute("aria-expanded") == "true"
        assert "Sign out" in menu.inner_text()
        page.keyboard.press("Escape")
        page.wait_for_timeout(100)
        assert not menu.is_visible() and btn.get_attribute("aria-expanded") == "false"
        assert page.evaluate("document.activeElement.id") == "account-menu-btn", "focus didn't come back"
        btn.click()
        menu.locator("[data-acct=accounts]").click()
        page.wait_for_function("() => location.hash === '#/accounts'", timeout=3000)
        page.wait_for_load_state("networkidle")
        assert not tab.errors, tab.errors
    finally:
        tab.close()


def test_the_safe_mode_pill_and_its_shortcut(browser, app_url):
    """Spec 019: the 18+ pill turns safe mode on (click) and off again (Shift + S), and says so."""
    tab = _Watched(browser, app_url, {"width": 1400, "height": 900})
    page = tab.page
    try:
        pill = page.locator("#sfw-toggle-btn")
        assert pill.get_attribute("aria-pressed") == "false"
        pill.click()
        page.wait_for_function("() => document.getElementById('sfw-toggle-btn').getAttribute('aria-pressed') === 'true'",
                               timeout=3000)
        page.locator("body").click(position={"x": 700, "y": 600})   # focus off the pill, onto the page
        page.keyboard.press("Shift+S")
        page.wait_for_function("() => document.getElementById('sfw-toggle-btn').getAttribute('aria-pressed') === 'false'",
                               timeout=3000)
        assert not tab.errors, tab.errors
    finally:
        tab.close()


def test_the_top_bar_controls_move_to_the_phone_bar(browser, app_url):
    """Spec 019: on a phone the one control cluster lives in #mobile-bar, and the menu still opens."""
    tab = _Watched(browser, app_url, {"width": 390, "height": 844})
    page = tab.page
    try:
        assert page.evaluate("!!document.querySelector('#mobile-bar #bar-controls')"), "controls not in the phone bar"
        page.locator("#account-menu-btn").click()
        assert page.locator("#account-menu").is_visible()
        assert not tab.errors, tab.errors
    finally:
        tab.close()


def test_the_posts_feed_switches_to_the_table_and_back(browser, app_url):
    """Spec 018: feed ↔ table, both drawing the same posts."""
    tab = _Watched(browser, app_url, {"width": 1400, "height": 900})
    page = tab.page
    try:
        tab.open("#/posts")
        for view in ("table", "feed"):
            page.locator(f"[data-view={view}]").first.click()
            page.wait_for_load_state("networkidle")
            page.wait_for_function("() => document.body.innerText.includes('Commissions open this weekend')",
                                   timeout=3000)
        assert not tab.errors, tab.errors
    finally:
        tab.close()


def test_the_composer_previews_what_you_type(browser, app_url):
    """Spec 018: what you type shows up in the per-site preview."""
    tab = _Watched(browser, app_url, {"width": 1400, "height": 900})
    page = tab.page
    try:
        tab.open("#/posts/new")
        page.locator("#post-body").fill("Testing the preview, fox 🦊")
        page.wait_for_function(
            "() => (document.getElementById('pp-preview') || {innerText: ''}).innerText.includes('Testing the preview')",
            timeout=5000)
        assert not tab.errors, tab.errors
    finally:
        tab.close()


def test_hiding_the_platforms_you_dont_use_sticks(browser, app_url):
    """Spec 020: "Hide all" on the not-set-up sites hides them, says so, and survives a reload."""
    tab = _Watched(browser, app_url, {"width": 1400, "height": 900})
    page = tab.page
    try:
        tab.open("#/platforms")
        page.locator("[data-act=hide-unset]").click()
        page.wait_for_selector(".ph-hidden-line", timeout=3000)
        assert not page.locator("#ph-unset-h").count(), "the not-set-up list is still showing"
        hidden = page.evaluate("fetch('/api/settings/preferences').then(r => r.json()).then(p => p.hidden_platforms)")
        assert hidden and not set(hidden) & set(SEEDED), f"hid the wrong ones: {hidden}"
        # A reload must still honour it. 4.54.6: the first page used to draw before the saved
        # preferences arrived, so about one load in three listed the hidden sites again — hence
        # several reloads, not one.
        for _ in range(3):
            page.reload(wait_until="networkidle")
            page.wait_for_selector(".ph-sec", timeout=5000)
            assert page.locator(".ph-hidden-line").count(), "after a reload the hidden platforms are back"
            assert not page.locator("#ph-unset-h").count(), "after a reload the not-set-up list is back"
        assert not tab.errors, tab.errors
    finally:
        tab.close()


@pytest.mark.parametrize("mode", ["default", "brut"])
def test_the_top_bar_keeps_the_bell_and_menu_on_screen(browser, app_url, mode):
    """4.54.6: in the top bar the links never shrink, so below ~1540px they pushed the bell and the
    account menu (Theme, Settings, Sign out) off the right edge — on most laptops. Every width from the
    phone breakpoint up must keep the 18+ pill, the bell and the account button fully on screen."""
    tab = _Watched(browser, app_url, {"width": 1920, "height": 900})
    page = tab.page
    try:
        if mode != "default":
            page.evaluate("m => { document.documentElement.dataset.mode = m; }", mode)
        fails = []
        for w in sorted(set(range(770, 1930, 10)) | {769, 899, 900, 1199, 1200, 1399, 1400, 1719, 1720}):
            page.set_viewport_size({"width": w, "height": 900})
            page.wait_for_timeout(30)
            off = page.evaluate("""() => ['sfw-toggle-btn', 'bell-slot', 'account-menu-btn'].filter(id => {
                const b = document.getElementById(id).getBoundingClientRect();
                return !(b.width > 0 && b.left >= 0 && b.right <= innerWidth && b.top >= 0 && b.bottom <= innerHeight);
            })""")
            if off:
                fails.append(f"{w}px: {', '.join(off)} off screen")
        assert not fails, "\n".join(fails)
    finally:
        tab.close()


def test_account_rows_fit_on_a_phone(browser, app_url):
    """ACCTROWS (phone screenshot, 2026-10-06): each account row ran off the right edge —
    Rename and the buttons after it were cut. Every control stays inside its card at 390px."""
    tab = _Watched(browser, app_url, {"width": 390, "height": 844})
    page = tab.page
    try:
        tab.open("#/accounts")
        page.wait_for_selector(".acct-card", timeout=5000)
        spill = page.evaluate("""() => {
            const out = [];
            for (const card of document.querySelectorAll('.acct-card')) {
                const edge = card.getBoundingClientRect().right + 0.5;
                for (const el of card.querySelectorAll('button, label, select, .acct-stat')) {
                    const r = el.getBoundingClientRect();
                    if (r.width && r.right > edge) out.push((el.textContent || el.tagName).trim().slice(0, 20));
                }
            }
            return out;
        }""")
        assert not spill, f"controls past the card's right edge: {spill}"
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1"), "the page scrolls sideways"
        assert not tab.errors, tab.errors
    finally:
        tab.close()


def test_the_phone_bar_covers_the_top_edge_when_scrolled(browser, app_url):
    """PHONEBAR2 (operator's iPhone screenshots, 2026-10-08): Library cards scrolled up behind the
    clock above the bar. The bar's backing is a real fixed element from the very top (iOS Safari
    fills its status-bar area from one and ignores pseudo-elements), solid, and nothing on the
    page shows through the top strip once scrolled."""
    tab = _Watched(browser, app_url, {"width": 390, "height": 844})
    page = tab.page
    try:
        tab.open("#/library")
        page.evaluate("window.scrollTo(0, 600)")
        page.wait_for_timeout(300)
        info = page.evaluate("""() => {
            const bg = document.getElementById('mobile-bar-bg');
            const cs = getComputedStyle(bg), r = bg.getBoundingClientRect();
            const hit = document.elementFromPoint(200, 4);
            return {pos: cs.position, top: r.top, h: r.height, w: r.width, color: cs.backgroundColor,
                    blur: cs.backdropFilter, hitIsBar: hit === bg || !!hit.closest('#mobile-bar, .hamburger-btn')};
        }""")
        assert info["pos"] == "fixed" and info["top"] == 0 and info["h"] >= 64 and info["w"] >= 389, info
        assert info["color"].startswith("rgb(") or info["color"].endswith(", 1)"), info   # opaque
        assert info["blur"] in ("none", ""), info
        assert info["hitIsBar"], "page content shows at the very top while scrolled"
        assert not tab.errors, tab.errors
    finally:
        tab.close()


def test_the_open_drawer_is_not_covered_on_a_phone(browser, app_url):
    """DRAWERTOP (operator's screenshot, 2026-10-08): with the menu open on the Library, the Filters
    pill covered the PawPoller name and the 18+ badge covered ☰, which moves to the drawer's edge
    to close it. While the drawer is open nothing sits on it, and ☰ can be tapped."""
    tab = _Watched(browser, app_url, {"width": 390, "height": 844})
    page = tab.page
    try:
        tab.open("#/library")
        page.evaluate("window.scrollTo(0, 900)")
        page.wait_for_timeout(400)
        page.locator(".hamburger-btn").click()
        page.wait_for_timeout(400)          # the drawer slides in
        out = page.evaluate("""() => {
            const at = (el) => { const r = el.getBoundingClientRect();
                return document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2); };
            const ham = document.querySelector('.hamburger-btn');
            const brand = document.querySelector('.sidebar .sidebar-header');
            const hit = at(brand);
            return {hamOnTop: at(ham)?.closest('.hamburger-btn') === ham,
                    brandOnTop: !!(hit && hit.closest('.sidebar')),
                    barHidden: getComputedStyle(document.getElementById('mobile-bar')).visibility === 'hidden'};
        }""")
        assert out["hamOnTop"], "☰ (the drawer's close button) is covered"
        assert out["brandOnTop"], "something covers the top of the open drawer"
        assert out["barHidden"], out
        page.locator(".hamburger-btn").click()
        page.wait_for_timeout(400)
        assert page.evaluate("getComputedStyle(document.getElementById('mobile-bar')).visibility") == "visible"
        assert not tab.errors, tab.errors
    finally:
        tab.close()


def test_the_age_question_and_the_under_18_lock(browser, app_url):
    """LEGALPAGES (4.58.0): an install with no answer asks once; answering "under 18" keeps safe mode on
    (the 18+ pill won't switch it off) and greys Mature / Adult in rating menus."""
    config.save_settings({"age_band": ""})
    tab = _Watched(browser, app_url, {"width": 1400, "height": 900})
    page = tab.page
    try:
        page.wait_for_selector("#age-ask", timeout=5000)
        page.locator("#age-ask-minor").click()
        page.wait_for_function("() => !document.getElementById('age-ask')", timeout=3000)
        assert page.evaluate("document.documentElement.dataset.ageLocked") == "1"
        assert page.evaluate("document.documentElement.dataset.sfw") == "1"
        # The pill is aria-disabled (Playwright won't click that), so press it as a tap would.
        assert page.evaluate("document.getElementById('sfw-toggle-btn').getAttribute('aria-disabled')") == "true"
        page.evaluate("document.getElementById('sfw-toggle-btn').click()")
        assert page.evaluate("document.documentElement.dataset.sfw") == "1", "the pill turned safe mode off"
        tab.open("#/posts/new")
        page.locator("#post-rating").focus()
        disabled = page.evaluate("[...document.querySelectorAll('#post-rating option')]"
                                 ".filter(o => o.disabled).map(o => o.value)")
        assert "general" not in disabled and set(disabled) >= {"mature", "adult"}, disabled
        assert not tab.errors, tab.errors
    finally:
        tab.close()
        config.save_settings({"age_band": "adult"})
