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
    """Since 4.53.0 (spec 019) Sign out is the last item of the account menu, in words,
    in red — the way most apps do it — rather than a button of its own in the bar."""

    def test_it_carries_the_words(self):
        item = HTML[HTML.index('data-acct="signout"'):][:400]
        assert ">Sign out<" in item

    def test_it_no_longer_relies_on_a_bare_arrow(self):
        assert 'id="logout-btn" title="Sign out">&#x2190;</button>' not in HTML

    def test_it_is_set_apart_and_red(self):
        assert ".acct-menu .acct-signout" in CSS
        rule = CSS[CSS.index(".acct-menu .acct-signout"):][:200]
        assert "var(--danger)" in rule


class TestTheDialog:
    def test_the_click_asks_before_it_acts(self):
        handler = APP[APP.index("async _signOut() {"):]
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
