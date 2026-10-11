"""The account email's codes and notices (4.68.0, spec 033 phase 6 / spec 023)."""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

import account_mail
import config

_KEYS = ["auth_username", "auth_password_hash", "auth_email", "auth_email_pending", "auth_code_confirm",
         "auth_code_reset", "auth_mail_log", "auth_session_secret", "legal_accepted", "legal_history"]
_LOCAL = ("127.0.0.1", 50000)


@pytest.fixture
def mails(monkeypatch):
    """Every mail the app would have sent, instead of sending it."""
    import techcentre
    monkeypatch.setattr(techcentre, "TECH_CENTRE_URL", "https://tc.example.invalid")
    out = []
    monkeypatch.setattr(account_mail, "_post", lambda to, purpose, code="": out.append((to, purpose, code)) or True)
    monkeypatch.setattr(account_mail, "notice", lambda purpose, to: out.append((to, purpose, "")))
    saved = {k: v for k, v in config.get_settings().items() if k in _KEYS}
    config.delete_settings_keys(_KEYS)
    config.invalidate_auth_required_cache()
    from routes import dashboard_auth
    dashboard_auth._RESET_ASKS.clear()
    yield out
    config.delete_settings_keys(_KEYS)
    if saved:
        config.save_settings(saved)
    config.invalidate_auth_required_cache()


def _code(mails):
    return mails[-1][2]


def test_a_code_works_once(mails):
    assert account_mail.send_code("confirm", "owner@example.com")
    code = _code(mails)
    assert len(code) == 6 and code.isdigit()
    stored = config.get_settings()["auth_code_confirm"]
    assert code not in str(stored)                      # only a fingerprint is kept
    assert account_mail.check_code("confirm", code) == "owner@example.com"
    assert account_mail.check_code("confirm", code) is None   # used up


def test_a_new_code_replaces_the_old(mails):
    account_mail.send_code("reset", "owner@example.com")
    old = _code(mails)
    account_mail.send_code("reset", "owner@example.com")
    new = _code(mails)
    if old != new:
        assert account_mail.check_code("reset", old) is None
    assert account_mail.check_code("reset", new) == "owner@example.com"


def test_five_wrong_tries_end_it(mails):
    account_mail.send_code("confirm", "owner@example.com")
    code = _code(mails)
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(account_mail.MAX_TRIES):
        assert account_mail.check_code("confirm", wrong) is None
    assert account_mail.check_code("confirm", code) is None


def test_an_old_code_expires(mails):
    account_mail.send_code("confirm", "owner@example.com")
    code = _code(mails)
    rec = config.get_settings()["auth_code_confirm"]
    rec["exp"] = time.time() - 1
    config.save_settings({"auth_code_confirm": rec})
    assert account_mail.check_code("confirm", code) is None


def test_three_sends_an_hour(mails):
    assert [account_mail.send_code("confirm", "owner@example.com") for _ in range(4)] == [True, True, True, False]


def test_not_ready_without_the_tech_centre(monkeypatch):
    import techcentre
    monkeypatch.setattr(techcentre, "TECH_CENTRE_URL", "")
    assert not account_mail.ready()
    assert not account_mail.send_code("confirm", "owner@example.com")


# ── The routes ───────────────────────────────────────────────────────────────

def _signed_up(c):
    import legal
    r = c.post("/api/auth/dashboard-setup", json={"username": "SecondFur", "email": "owner@example.com",
                                                   "password": "correct horse", "confirm": "correct horse"})
    assert r.status_code == 200
    legal.accept(legal.current()["terms"], legal.current()["privacy"])


def _client():
    import dashboard
    return TestClient(dashboard.app, client=_LOCAL)


def test_signup_sends_the_confirm_code_and_it_confirms(mails):
    c = _client()
    _signed_up(c)
    assert mails and mails[-1][:2] == ("owner@example.com", "confirm")
    r = c.post("/api/auth/email/confirm", json={"code": "999999" if _code(mails) != "999999" else "888888"})
    assert r.status_code == 400
    assert c.post("/api/auth/email/confirm", json={"code": _code(mails)}).status_code == 200
    s = config.get_settings()
    assert s["auth_email"] == "owner@example.com" and not s.get("auth_email_pending")
    assert c.get("/api/auth/signup-status").json()["email"] == "confirmed"
    assert mails[-1] == ("owner@example.com", "verified", "")   # "You've been verified" (4.69.1)


def test_changing_a_confirmed_email_tells_the_old_one(mails):
    c = _client()
    _signed_up(c)
    c.post("/api/auth/email/confirm", json={"code": _code(mails)})
    c.post("/api/auth/email", json={"email": "new@example.com", "current_password": "correct horse"})
    assert mails[-1][:2] == ("new@example.com", "confirm")
    c.post("/api/auth/email/confirm", json={"code": _code(mails)})
    assert ("owner@example.com", "email_changed", "") in mails
    assert config.get_settings()["auth_email"] == "new@example.com"


def test_reset_by_code(mails):
    c = _client()
    _signed_up(c)
    c.post("/api/auth/email/confirm", json={"code": _code(mails)})
    old_secret = config.get_settings().get("auth_session_secret")
    # Same answer for a stranger as for the owner.
    a = c.post("/api/auth/reset-request", json={"who": "nobody@example.com"})
    b = c.post("/api/auth/reset-request", json={"who": "OWNER@example.com"})
    assert a.status_code == b.status_code == 200 and a.json() == b.json()
    deadline = time.time() + 5   # the send runs in the background
    while mails[-1][1] != "reset" and time.time() < deadline:
        time.sleep(0.02)
    assert mails[-1][:2] == ("owner@example.com", "reset")
    bad = c.post("/api/auth/reset", json={"code": "12", "password": "new password 1", "confirm": "new password 1"})
    assert bad.status_code == 400
    r = c.post("/api/auth/reset", json={"code": _code(mails), "password": "new password 1",
                                        "confirm": "new password 1"})
    assert r.status_code == 200
    assert config.verify_password("new password 1", config.get_settings()["auth_password_hash"])
    assert config.get_settings().get("auth_session_secret") != old_secret   # every session ended
    assert ("owner@example.com", "password_changed", "") in mails


def test_sign_in_with_the_confirmed_email(mails):
    c = _client()
    _signed_up(c)
    body = {"username": "Owner@Example.com", "password": "correct horse"}
    assert c.post("/api/auth/dashboard-login", json=body).status_code == 401   # not confirmed yet
    c.post("/api/auth/email/confirm", json={"code": _code(mails)})
    assert c.post("/api/auth/dashboard-login", json=body).status_code == 200


def test_the_server_wants_an_email(mails, monkeypatch):
    """Older installs: a session with no email at all gets only the sign-up routes."""
    import dashboard
    monkeypatch.setattr(dashboard, "SIGNUP_GATES", True)
    c = _client()
    _signed_up(c)
    config.delete_settings_keys(["auth_email", "auth_email_pending"])
    r = c.get("/api/settings/preferences")
    assert r.status_code == 403 and r.json()["error"] == "email"
    assert c.get("/api/auth/signup-status").status_code == 200


def test_reset_works_signed_out(mails):
    """Forgot your password is used by someone who can't sign in: no session cookie."""
    c = _client()
    _signed_up(c)
    c.post("/api/auth/email/confirm", json={"code": _code(mails)})
    out = _client()                                   # a fresh browser, signed out
    assert out.post("/api/auth/reset-request", json={"who": "SecondFur"}).status_code == 200
    deadline = time.time() + 5
    while mails[-1][1] != "reset" and time.time() < deadline:
        time.sleep(0.02)
    r = out.post("/api/auth/reset", json={"code": _code(mails), "password": "new password 2",
                                          "confirm": "new password 2"})
    assert r.status_code == 200


def test_parallel_guesses_still_get_five_tries(mails):
    """/api/auth/reset is reachable signed out and runs in a thread pool: the try count can't race."""
    from concurrent.futures import ThreadPoolExecutor
    account_mail.send_code("reset", "owner@example.com")
    code = _code(mails)
    wrong = [f"{(int(code) + i) % 10 ** 6:06d}" for i in range(1, 41)]
    with ThreadPoolExecutor(20) as ex:
        hits = list(ex.map(lambda g: account_mail.check_code("reset", g), wrong))
    assert not any(hits)
    assert account_mail.check_code("reset", code) is None      # five wrong tries ended it


def test_reset_requests_have_their_own_limit(mails):
    c = _client()
    codes = [c.post("/api/auth/reset-request", json={"who": "nobody"}).status_code for _ in range(6)]
    assert codes == [200] * 5 + [429]
    from dashboard import _is_rate_limited
    assert not _is_rate_limited("127.0.0.1")                 # asking isn't a failed sign-in


def test_a_reset_code_dies_when_the_email_changes(mails):
    c = _client()
    _signed_up(c)
    c.post("/api/auth/email/confirm", json={"code": _code(mails)})
    account_mail.send_code("reset", "owner@example.com")
    code = _code(mails)
    config.save_settings({"auth_email": "new@example.com"})
    r = c.post("/api/auth/reset", json={"code": code, "password": "new password 3", "confirm": "new password 3"})
    assert r.status_code == 400


def test_a_failed_send_does_not_use_up_the_hour(mails, monkeypatch):
    """4.69.1: every send failed (mail refused), and those failures locked the owner out for an hour."""
    monkeypatch.setattr(account_mail, "_post", lambda *a, **k: False)
    assert [account_mail.send_code("confirm", "owner@example.com") for _ in range(5)] == [False] * 5
    monkeypatch.setattr(account_mail, "_post", lambda to, purpose, code="": mails.append((to, purpose, code)) or True)
    assert account_mail.send_code("confirm", "owner@example.com")


def test_a_code_that_never_went_out_cannot_be_used(mails, monkeypatch):
    """4.69.1 review: a refunded send that kept its code let forced failures mint unlimited guessable codes."""
    made = []
    monkeypatch.setattr(account_mail, "_post", lambda to, purpose, code="": made.append(code) and False)
    assert not account_mail.send_code("reset", "owner@example.com")
    assert made and account_mail.check_code("reset", made[-1]) is None


def test_a_code_is_not_valid_while_it_is_being_sent(mails, monkeypatch):
    """4.69.1 re-review: saved before the send, a code could be guessed during the Tech Centre round trip."""
    seen = []
    monkeypatch.setattr(account_mail, "_post",
                        lambda to, purpose, code="": seen.append(account_mail.check_code(purpose, code)) or True)
    assert account_mail.send_code("reset", "owner@example.com")
    assert seen == [None]
