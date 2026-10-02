"""Spec 019 — the top bar: three clear groups, an 18+ safe-mode pill, an account menu.

The right end of the bar held six controls in four styles: emoji search, a padlock for
safe mode (it read as "security", not "adult content hidden"), "?", a palette, the only
labelled one ("Sign out") and a bell placed on its own with position: fixed. Now: a
search field, the clock, the 18+ pill, the bell in its slot and the avatar, whose menu
holds the rarely used things. One outline icon set; no emoji in the bar.
"""
from __future__ import annotations

import re

HTML = open("frontend/index.html", encoding="utf-8").read()
APP = open("frontend/js/app.js", encoding="utf-8").read()
NOTIF = open("frontend/js/notifications_center.js", encoding="utf-8").read()
TOUR = open("frontend/js/tour.js", encoding="utf-8").read()
LAYOUT = open("frontend/css/layout.css", encoding="utf-8").read()
SAFE = open("frontend/css/safe_mode.css", encoding="utf-8").read()

# Pictographs and the emoji presentation selector — what the old bar was made of.
_EMOJI = re.compile("[\U0001F300-\U0001FAFF☀-➿️]|&#1[23][0-9]{4};")


def _block(start: str, end: str) -> str:
    i = HTML.index(start)
    return HTML[i:HTML.index(end, i)]


class TestOneIconSet:
    def test_one_sprite(self):
        assert HTML.count('id="pp-icons"') == 1
        sprite = _block('id="pp-icons"', "</svg>")
        for name in ("search", "bell", "palette", "map", "help", "user", "gear", "out", "caret"):
            assert f'<symbol id="i-{name}"' in sprite, name

    def test_the_bar_controls_use_it_and_carry_no_emoji(self):
        bar = _block('id="bar-controls"', "<!-- /bar-controls -->")
        search = _block('id="sidebar-search"', "</button>")
        assert '<use href="#i-' in bar and '<use href="#i-search"' in search
        assert not _EMOJI.search(bar), _EMOJI.search(bar)
        assert not _EMOJI.search(search), _EMOJI.search(search)

    def test_the_bell_uses_it_too(self):
        assert '<use href="#i-bell"' in NOTIF
        assert "🔔" not in NOTIF


class TestTheOldButtonsAreMenuItems:
    def test_the_three_buttons_are_gone(self):
        for old in ('id="help-tour-btn"', 'id="theme-toggle-btn"', 'id="logout-btn"'):
            assert old not in HTML, old
        for old in ("getElementById('help-tour-btn')", "getElementById('theme-toggle-btn')",
                    "getElementById('logout-btn')"):
            assert old not in APP, old

    def test_their_actions_live_in_the_account_menu(self):
        menu = _block('id="account-menu"', "<!-- /account-menu -->")
        for act in ("theme", "tour", "help", "accounts", "settings", "signout"):
            assert f'data-acct="{act}"' in menu, act
        assert menu.index('data-acct="signout"') > menu.index('data-acct="settings"'), "Sign out is last"
        assert 'role="menu"' in menu and menu.count('role="menuitem"') >= 6

    def test_it_is_a_real_menu_button(self):
        btn = _block('id="account-menu-btn"', "</button>")
        assert 'aria-haspopup="menu"' in btn and 'aria-expanded="false"' in btn
        body = APP[APP.index("_initAccountMenu() {"):][:6000]
        for key in ("'ArrowDown'", "'ArrowUp'", "'Escape'", "'Home'", "'End'"):
            assert key in body, key
        assert "focus()" in body

    def test_the_tour_no_longer_points_at_the_old_button(self):
        assert "#help-tour-btn" not in TOUR
        assert "#account-menu-btn" in TOUR


class TestSafeModePill:
    def test_the_pill_says_its_state(self):
        sync = APP[APP.index("const _syncSfwBtn = () => {"):][:1500]
        assert "'Safe'" in sync and "'Shown'" in sync
        assert "aria-label" in sync and "aria-pressed" in sync

    def test_same_storage_key_and_pre_paint(self):
        assert "localStorage.setItem('pawpoller-sfw'" in APP
        assert "pawpoller-sfw" in HTML[:HTML.index("</head>")], "applied before first paint"

    def test_shift_s_toggles_but_not_while_typing(self):
        body = APP[APP.index("_isTypingTarget(el) {"):][:600]
        assert "isContentEditable" in body and "INPUT" in body and "TEXTAREA" in body and "SELECT" in body
        key = APP[APP.index("/* Shift + S"):][:900]
        assert "e.shiftKey" in key and "_isTypingTarget(" in key
        assert "e.ctrlKey" in key and "e.metaKey" in key and "e.altKey" in key

    def test_the_toast_names_the_shortcut(self):
        assert "Shift + S" in APP[APP.index("const _toggleSfw = () => {"):][:1600]

    def test_both_states_are_coloured(self):
        assert 'html[data-sfw="1"] .sfw-pill' in SAFE
        assert ".sfw-pill .sfw-badge" in SAFE


class TestTheBell:
    def test_it_mounts_into_the_bar_slot(self):
        body = NOTIF[NOTIF.index("function ensureEls()"):][:1600]
        assert "getElementById('bell-slot')" in body
        assert "document.body.appendChild(_bell)" in body, "fallback where no slot exists"

    def test_in_the_slot_it_is_not_fixed(self):
        css = open("frontend/css/loading_indicator.css", encoding="utf-8").read()
        assert ".bell-slot .pp-notif-bell" in css
        rule = css[css.index(".bell-slot .pp-notif-bell"):][:400]
        assert "position: relative" in rule and "position: fixed" not in rule


class TestPhone:
    def test_one_cluster_moves_into_the_phone_bar(self):
        assert 'id="mobile-bar"' in HTML
        assert HTML.index('id="mobile-bar"') < HTML.index('<nav class="sidebar"'), \
            "outside the sidebar: a transformed ancestor would break position: fixed"
        body = APP[APP.index("_placeBarControls() {"):][:900]
        assert "mobile-bar" in body and "sidebar-footer" in body

    def test_phone_buttons_are_44px(self):
        assert ".mobile-bar" in LAYOUT
        block = LAYOUT[LAYOUT.index("/* ── Phone bar"):][:3000]
        assert "44px" in block
