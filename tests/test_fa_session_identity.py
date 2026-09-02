""""Logged in" was one question short: logged in as WHO? (3.31.0)

Reported as three sets of renewed FurAffinity cookies pasted through the
Accounts → Credentials panel with nothing changing, and no explanation
available anywhere in the app.

The save path turned out to be correct — three distinct values were stored
under three correct per-account keys. What was missing was any way to say why
two of them still did not work. `validate_cookies` answers *"is somebody logged
in"*, and FurAffinity keeps **one session per browser**: copying cookies while
signed in as a different account yields a perfectly valid session for the wrong
user. Every check in the codebase said yes to that, so the only advice the UI
could give was "re-copy your cookies" — which reproduces the same result
forever.

⚠ Getting this wrong is not cosmetic. The poller files whichever gallery the
session can see under the account it *thinks* it polled, and the poster uploads
to whichever account the session belongs to rather than the one selected. With
a friend's account in the list, "post as account X" silently becoming "post as
account Y" is the failure that must not happen.

Measured against a live logged-in page, FA marks the signed-in user in the
mobile nav on every authenticated page:

    <img class="loggedin_user_avatar avatar" alt="ThirdFur" src="...">

Note the **two-token class**. A pattern anchored on the closing quote after
`loggedin_user_avatar` matches nothing, which is indistinguishable from "no
marker" and would manufacture a false mismatch — so the no-marker case is
deliberately treated as *unconfirmed*, never as *wrong*.
"""
from __future__ import annotations

import asyncio

import pytest

from clients.fa.client import _logged_in_username, _same_fa_user

NAV = ('<a href="/user/{u}/"><img class="loggedin_user_avatar avatar" '
       'alt="{d}" src="//a.furaffinity.net/1/{u}.gif"/></a>'
       '<h2><a href="/user/{u}/">{d}</a></h2>')

LOGGED_OUT = ('<html><body><form action="/login/"><input name="name"></form>'
              '<p>You must log in to view this page.</p></body></html>')


def _page(user, display=None):
    return ('<html><body><nav><a href="/logout/?key=abc">Log Out</a></nav>'
            + NAV.format(u=user, d=display or user) + '</body></html>')


NO_MARKER = ('<html><body><nav><a href="/logout/?key=abc">Log Out</a></nav>'
             '<div id="submissions"></div></body></html>')


class _Resp:
    def __init__(self, text, status=200):
        self.text, self.status_code = text, status


def _client(monkeypatch, page, status=200, username="SecondFur"):
    from clients.fa.client import FAClient
    c = FAClient(username=username, cookie_a="a", cookie_b="b")

    class _Http:
        async def get(self, url, **kw):
            return _Resp(page, status)

    async def _fake():
        return _Http()

    monkeypatch.setattr(c, "_get_fa_http", _fake)
    monkeypatch.setattr(c, "close", _fake)
    return c


# ── reading the marker ───────────────────────────────────────────────

def test_the_username_is_read_from_the_avatar():
    """THE parse. The class carries a second token — a pattern that stops at
    the closing quote finds nothing and looks exactly like "logged out"."""
    assert _logged_in_username(_page("thirdfur", "ThirdFur")) == "ThirdFur"


def test_the_userpage_link_is_the_fallback():
    html = ('<a href="/user/secondhandle/"><img class="loggedin_user_avatar avatar" '
            'src="//a/x.gif"/></a>')
    assert _logged_in_username(html) == "secondhandle"


def test_a_logged_out_page_names_nobody():
    assert _logged_in_username(LOGGED_OUT) == ""
    assert _logged_in_username("") == ""


# ── comparing two spellings of one account ───────────────────────────

@pytest.mark.parametrize("a,b", [
    ("SecondFur", "secondfur"),          # display case vs URL case
    ("Second_Fur", "secondfur"),         # FA drops underscores in URLs
    ("SECONDFUR", "Second_Fur"),
    (" secondfur ", "SecondFur"),
])
def test_two_spellings_of_one_account_match(a, b):
    """FA lowercases and drops underscores in URLs while the display name keeps
    both. A literal comparison would report a mismatch between two spellings of
    the same account and lock someone out of their own credentials."""
    assert _same_fa_user(a, b) is True


@pytest.mark.parametrize("a,b", [
    ("SecondFur", "ThirdFur"),
    ("", "SecondFur"),
    ("SecondFur", ""),
])
def test_genuinely_different_users_do_not_match(a, b):
    assert _same_fa_user(a, b) is False


# ── the session check ────────────────────────────────────────────────

def test_the_right_account_passes(monkeypatch):
    c = _client(monkeypatch, _page("secondfur", "SecondFur"), username="SecondFur")
    r = asyncio.run(c.validate_session())
    assert r["ok"] and r["logged_in"] and r["matches"]
    assert r["username"] == "SecondFur"


def test_the_wrong_account_is_a_distinct_answer(monkeypatch):
    """THE regression. Logged in, valid cookies, wrong person — and the old
    check returned plain True."""
    c = _client(monkeypatch, _page("thirdfur", "ThirdFur"), username="SecondFur")
    r = asyncio.run(c.validate_session())
    assert r["logged_in"] is True
    assert r["matches"] is False
    assert r["ok"] is False


def test_the_wrong_account_message_names_both_and_says_what_to_do(monkeypatch):
    """"Invalid" sends someone back to re-copy the same cookies from the same
    browser, which is what happened three times."""
    c = _client(monkeypatch, _page("thirdfur", "ThirdFur"), username="SecondFur")
    detail = asyncio.run(c.validate_session())["detail"]
    assert "ThirdFur" in detail and "SecondFur" in detail
    assert "one session per browser" in detail


def test_a_logged_out_session_fails_and_is_not_a_mismatch(monkeypatch):
    c = _client(monkeypatch, LOGGED_OUT)
    r = asyncio.run(c.validate_session())
    assert r["ok"] is False and r["logged_in"] is False
    assert r["matches"] is False
    assert "no longer authenticates" in r["detail"]


def test_the_dead_cookie_message_names_the_same_browser_trap(monkeypatch):
    """The measured cause, not a generic "expired".

    FA keeps ONE session per browser and rotates cookie `a` on each sign-in
    while `b` persists — verified live, where three accounts held three
    different `a` values against one identical `b` and only the
    last-renewed pair still worked. "Cookies expired" sends someone back to
    the same browser to copy the same dead pair; naming the trap does not.
    """
    c = _client(monkeypatch, LOGGED_OUT, username="SecondFur")
    detail = asyncio.run(c.validate_session())["detail"]
    assert "one browser" in detail
    assert "private window" in detail
    assert "SecondFur" in detail


def test_a_missing_marker_is_unconfirmed_never_wrong(monkeypatch):
    """A parse miss must not manufacture a mismatch — that would lock a user
    out of working cookies on a page-layout change."""
    c = _client(monkeypatch, NO_MARKER, username="SecondFur")
    r = asyncio.run(c.validate_session())
    assert r["logged_in"] is True
    assert r["ok"] is True
    assert "unconfirmed" in r["detail"]


def test_no_cookies_is_reported_not_crashed(monkeypatch):
    from clients.fa.client import FAClient
    r = asyncio.run(FAClient(username="SecondFur", cookie_a="", cookie_b="").validate_session())
    assert r["ok"] is False and r["detail"]


def test_a_non_200_is_not_a_pass(monkeypatch):
    c = _client(monkeypatch, _page("secondfur"), status=503)
    r = asyncio.run(c.validate_session())
    assert r["ok"] is False and "503" in r["detail"]


def test_validate_cookies_still_answers_the_old_question(monkeypatch):
    """Its four callers (poller, poster, connect, test-login) keep working."""
    c = _client(monkeypatch, _page("anyone"))
    assert asyncio.run(c.validate_cookies()) is True
    c = _client(monkeypatch, LOGGED_OUT)
    assert asyncio.run(c.validate_cookies()) is False


# ── what the endpoints do with it ────────────────────────────────────

def _src(rel):
    from pathlib import Path
    return (Path(__file__).resolve().parent.parent / rel).read_text(
        encoding="utf-8", errors="replace")


def test_the_test_login_endpoint_reports_the_wrong_account_case():
    body = _src("routes/settings_api.py")
    assert '"status": "wrong_account"' in body
    assert "validate_session()" in body


def test_the_connect_form_refuses_a_wrong_account_paste():
    """Storing it would point polling at one gallery and posting at another."""
    body = _src("routes/fa_api.py")
    assert "validate_session()" in body
    assert 'res["logged_in"] and not res["matches"]' in body


# ── the panel the credentials are typed into ─────────────────────────

def _js(name):
    from pathlib import Path
    return (Path(__file__).resolve().parent.parent / "frontend" / "js" /
            name).read_text(encoding="utf-8", errors="replace")


def _code_only(src):
    import re
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"(?<!:)//.*", "", src)


def _method(src, signature):
    """Body of a method by its DEFINITION, not its first mention.

    Anchoring on the bare name found the call site in the event wiring and read
    the wrong 3,000 characters — a test that fails for a reason unrelated to
    what it is checking is worse than no test.
    """
    return src[src.index(signature):]


def test_the_panel_names_the_account_it_edits():
    """It opened headed "this account" with no name on it. With several
    accounts on one platform that is a coin flip the user cannot check."""
    src = _code_only(_js("accounts.js"))
    fn = _method(src, "_editCredentials(accountId, platform, btn) {")
    assert "acctName" in fn[:2000]


def test_a_successful_save_closes_the_panel():
    """The ask: "needs a better way of showing it saved, by maybe closing that
    new creds tab thing". Leaving it open with one line of muted text read as
    nothing having happened."""
    src = _code_only(_js("accounts.js"))
    fn = _method(src, "_editCredentials(accountId, platform, btn) {")
    ok = fn[fn.index("status === 'ok'"):]
    assert "panel.remove()" in ok[:600]


def test_a_failed_save_keeps_the_panel_open():
    """The values ARE stored but do not work, and re-pasting is the next step —
    closing the form would hide the one control needed to fix it."""
    src = _code_only(_js("accounts.js"))
    fn = _method(src, "_editCredentials(accountId, platform, btn) {")
    seg = fn[fn.index("wrong_account"):]
    seg = seg[:seg.index("_flashSaved") if "_flashSaved" in seg else 800]
    assert "panel.remove()" not in seg


def test_the_result_outlives_the_panel():
    src = _code_only(_js("accounts.js"))
    assert "_setTestStatus" in src and "_flashSaved" in src


def test_the_row_status_distinguishes_wrong_account_from_expired():
    src = _code_only(_js("accounts.js"))
    fn = _method(src, "async _testLogin(accountId) {")
    assert "wrong_account" in fn[:1600]


def test_every_helper_accounts_js_calls_is_defined():
    """The guard that has caught `API.testAccountLogin`, `this._toast` and
    `App.showToast` shipped undefined."""
    import re
    src = _code_only(_js("accounts.js"))
    defined_api = set(re.findall(r"^\s{4}(?:async\s+)?(\w+)\s*\(", _js("api.js"), re.M))
    defined_app = set(re.findall(r"^\s{4}(?:async\s+)?(\w+)\s*\(", _js("app.js"), re.M))
    defined_self = set(re.findall(r"^\s{4}(?:async\s+)?(\w+)\s*\(", src, re.M))
    missing_api = sorted(set(re.findall(r"API\.(\w+)\s*\(", src)) - defined_api)
    missing_app = sorted(set(re.findall(r"App\.(\w+)\s*\(", src)) - defined_app)
    missing_self = sorted(set(re.findall(r"this\.(_\w+)\s*\(", src)) - defined_self)
    assert missing_api == [], f"undefined API methods: {missing_api}"
    assert missing_app == [], f"undefined App methods: {missing_app}"
    assert missing_self == [], f"undefined own methods: {missing_self}"
