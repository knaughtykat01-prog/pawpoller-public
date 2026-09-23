"""Signing out should look like signing out, and should ask (backlog SIGNOUT).

It was a 30x30 unlabelled "←" between two other glyph buttons, with the only explanation
in a tooltip, and one click ended the session outright — on a phone that arrow is exactly
what a thumb reaches for when it means "back".
"""
from __future__ import annotations

APP = open("frontend/js/app.js", encoding="utf-8").read()
HTML = open("frontend/index.html", encoding="utf-8").read()
CSS = open("frontend/css/layout.css", encoding="utf-8").read()


class TestTheButton:
    def test_it_carries_the_words(self):
        assert 'class="btn-logout-label">Sign out<' in HTML
        assert 'aria-label="Sign out"' in HTML

    def test_it_no_longer_relies_on_a_bare_arrow(self):
        assert 'id="logout-btn" title="Sign out">&#x2190;</button>' not in HTML

    def test_it_gets_its_own_row_but_shrinks_with_the_sidebar(self):
        block = CSS[CSS.index(".btn-logout {"):]
        assert "flex: 1 1 100%" in block[:400]
        assert ".sidebar.collapsed .btn-logout-label { display: none; }" in CSS


class TestTheDialog:
    def test_the_click_asks_before_it_acts(self):
        handler = APP[APP.index("document.getElementById('logout-btn')"):]
        head = handler[:700]
        assert "await this._confirmSignOut(" in head
        assert head.index("_confirmSignOut") < head.index("dashboardLogout"), \
            "the confirmation must come before the session is ended"

    def test_staying_signed_in_is_the_safe_default(self):
        body = APP[APP.index("_confirmSignOut(dashboardAuth)"):]
        body = body[:2200]
        assert "e.key === 'Escape'" in body and "close(false)" in body
        assert "if (e.target === ov) close(false)" in body     # backdrop click

    def test_it_says_which_sign_out_this_is(self):
        body = APP[APP.index("_confirmSignOut(dashboardAuth)"):][:2200]
        assert "need your password to get back in" in body    # dashboard session
        assert "clears the site login" in body                # no dashboard password set

    def test_it_promises_polling_keeps_running(self):
        """The fear that stops people signing out: 'does my stats collection stop?'"""
        body = APP[APP.index("_confirmSignOut(dashboardAuth)"):][:2200]
        assert "keeps checking your sites" in body
