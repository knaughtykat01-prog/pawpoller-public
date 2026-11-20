"""Auth hardening (gap-wave-4): 2FA backup codes, setup loopback gate,
require-password-to-enable, constant-time API keys."""
import config
import routes.dashboard_auth as da
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _client():
    app = FastAPI()
    app.include_router(da.dashboard_auth_router)
    return TestClient(app)


def _configure(pw="pw12345678"):
    config.save_settings({"auth_username": "admin",
                          "auth_password_hash": config.hash_password(pw)})


def _enable_2fa(c, pw="pw12345678"):
    import pyotp
    secret = c.post("/api/auth/totp-setup").json()["secret"]
    code = pyotp.TOTP(secret).now()
    return c.post("/api/auth/totp-enable", json={"code": code, "password": pw}).json()


def test_backup_code_consume_is_case_and_dash_insensitive():
    plain, hashes = da._generate_backup_codes()
    assert len(plain) == len(hashes) == 10
    config.save_settings({"auth_totp_backup_codes": hashes})
    # Upper-cased + dashes stripped still matches, and consuming removes it.
    assert da._consume_backup_code(plain[0].upper().replace("-", ""), config.get_settings())
    assert len(config.get_settings()["auth_totp_backup_codes"]) == 9
    # A used code can't be reused.
    assert da._consume_backup_code(plain[0], config.get_settings()) is False


def test_totp_enable_requires_password_and_issues_codes():
    _configure()
    import pyotp
    c = _client()
    secret = c.post("/api/auth/totp-setup").json()["secret"]
    code = pyotp.TOTP(secret).now()
    # Wrong password is rejected even with a valid code.
    assert c.post("/api/auth/totp-enable", json={"code": code, "password": "nope"}).status_code == 401
    r = c.post("/api/auth/totp-enable", json={"code": code, "password": "pw12345678"})
    assert r.status_code == 200
    assert len(r.json()["backup_codes"]) == 10
    assert config.get_settings().get("auth_totp_enabled") is True


def test_login_accepts_a_backup_code_once():
    _configure()
    c = _client()
    codes = _enable_2fa(c)["backup_codes"]
    ok = c.post("/api/auth/dashboard-login",
                json={"username": "admin", "password": "pw12345678", "totp_code": codes[0]})
    assert ok.status_code == 200
    # The same backup code can't be replayed.
    again = c.post("/api/auth/dashboard-login",
                   json={"username": "admin", "password": "pw12345678", "totp_code": codes[0]})
    assert again.status_code == 401
    assert len(config.get_settings()["auth_totp_backup_codes"]) == 9


def test_disable_accepts_backup_code_when_authenticator_lost():
    _configure()
    c = _client()
    codes = _enable_2fa(c)["backup_codes"]
    # No live TOTP code — a backup code + password still disables (recovery path).
    r = c.post("/api/auth/totp-disable", json={"password": "pw12345678", "code": codes[1]})
    assert r.status_code == 200
    assert not config.get_settings().get("auth_totp_enabled")
    assert not config.get_settings().get("auth_totp_backup_codes")   # cleared


def test_setup_gate_refuses_remote_allows_override(monkeypatch):
    c = _client()   # unconfigured (fresh per-test settings)
    body = {"username": "admin", "password": "password1", "confirm": "password1"}
    # TestClient's client host is "testclient" (not loopback) → refused.
    r = c.post("/api/auth/dashboard-setup", json=body)
    assert r.status_code == 403 and "localhost" in r.json()["detail"].lower()
    assert not config.get_settings().get("auth_password_hash")   # nothing set
    # Conscious opt-in env allows it.
    monkeypatch.setenv("PAWPOLLER_ALLOW_OPEN_SETUP", "1")
    assert c.post("/api/auth/dashboard-setup", json={**body, "username": "SecondFur",
                                                      "email": "owner@example.com"}).status_code == 200
    assert config.get_settings().get("auth_password_hash")


def test_api_key_validation_still_works_constant_time():
    _configure()
    c = _client()
    key = c.post("/api/auth/api-keys", json={"name": "test"}).json()["key"]
    assert config.validate_api_key(key) is True
    assert config.validate_api_key("pp_deadbeef00") is False
    assert config.validate_api_key("not-a-key") is False


# ── 4.43.2 test audit: the three auth flows that had no test at all ──────────

def test_changing_the_password_checks_everything_and_signs_everyone_out():
    """ASVS V7.4.3: a password change rotates the session secret, so every other session
    (and a stolen cookie) stops working. Untested until the 2026-09-29 test audit."""
    _configure()
    c = _client()
    url = "/api/auth/dashboard-change-password"
    assert c.post(url, json={"current_password": "pw12345678", "new_password": "short", "confirm": "short"}).status_code == 400
    assert c.post(url, json={"current_password": "pw12345678", "new_password": "newpass123",
                             "confirm": "different1"}).status_code == 400
    assert c.post(url, json={"current_password": "wrong-one", "new_password": "newpass123",
                             "confirm": "newpass123"}).status_code == 401
    before = config.get_or_create_session_secret()
    r = c.post(url, json={"current_password": "pw12345678", "new_password": "newpass123", "confirm": "newpass123"})
    assert r.status_code == 200
    stored = config.get_settings()["auth_password_hash"]
    assert config.verify_password("newpass123", stored) and not config.verify_password("pw12345678", stored)
    assert config.get_or_create_session_secret() != before          # every session signed with the old one is dead


def test_regenerating_backup_codes_needs_the_password_and_kills_the_old_codes():
    _configure()
    c = _client()
    url = "/api/auth/totp-backup-codes/regenerate"
    assert c.post(url, json={"password": "pw12345678"}).status_code == 400    # 2FA not on yet
    old = _enable_2fa(c)["backup_codes"]
    assert c.post(url, json={"password": "wrong"}).status_code == 401
    r = c.post(url, json={"password": "pw12345678"})
    assert r.status_code == 200
    new = r.json()["backup_codes"]
    assert len(new) == 10 and not set(new) & set(old)
    assert da._consume_backup_code(old[0], config.get_settings()) is False   # the old set is dead
    assert da._consume_backup_code(new[0], config.get_settings()) is True


def test_a_revoked_api_key_stops_working_at_once():
    _configure()
    c = _client()
    made = c.post("/api/auth/api-keys", json={"name": "paired desktop"}).json()
    assert config.validate_api_key(made["key"])
    assert c.delete(f"/api/auth/api-keys/{made['prefix']}").status_code == 200
    assert not config.validate_api_key(made["key"])
    assert c.delete(f"/api/auth/api-keys/{made['prefix']}").status_code == 404


def test_a_code_with_a_space_or_dash_in_it_still_works():
    """Authenticator apps show a code as "123 456", and some copy it that way (or with a no-break space): the gap
    isn't part of the code. Turning 2FA on and signing in both take it; a code that's still wrong is still refused."""
    _configure()
    import pyotp
    c = _client()
    secret = c.post("/api/auth/totp-setup").json()["secret"]
    code = pyotp.TOTP(secret).now()
    r = c.post("/api/auth/totp-enable", json={"code": f"{code[:3]} {code[3:]}", "password": "pw12345678"})
    assert r.status_code == 200, r.text
    code = pyotp.TOTP(secret).now()
    for typed in (f"{code[:3]} {code[3:]}", f"{code[:3]}-{code[3:]}", f" {code} "):
        ok = c.post("/api/auth/dashboard-login", json={"username": "admin", "password": "pw12345678", "totp_code": typed})
        assert ok.status_code == 200, repr(typed)
    wrong = str((int(code) + 500000) % 1000000).zfill(6)
    bad = c.post("/api/auth/dashboard-login", json={"username": "admin", "password": "pw12345678", "totp_code": f"{wrong[:3]} {wrong[3:]}"})
    assert bad.status_code == 401


def test_a_backup_code_typed_with_a_space_still_works():
    plain, hashes = da._generate_backup_codes()
    config.save_settings({"auth_totp_backup_codes": hashes})
    assert da._consume_backup_code(plain[0].replace("-", " "), config.get_settings())


def test_the_code_boxes_fit_a_backup_code():
    """A backup code is nine characters ("1a2b-3c4d"). A six-character box cut it to "1a2b-3", so it could never sign
    in, and cut a pasted "123 456" to "123 45". The sign-in box also takes letters, which a backup code has."""
    import re
    from pathlib import Path
    js = (Path(__file__).resolve().parent.parent / "frontend" / "js" / "app.js").read_text(encoding="utf-8")
    tag = lambda box: re.search(r'<input[^>]*id="' + box + r'"[^>]*>', js).group(0)
    for box in ("dash-login-totp", "signup-totp", "totp-verify-code"):
        n = re.search(r'maxlength="(\d+)"', tag(box))
        assert not n or int(n.group(1)) >= 11, box
    assert 'inputmode="numeric"' not in tag("dash-login-totp")


def _wrong_everywhere(secret):
    """A six-digit code that isn't right at any time the clock check looks at."""
    for n in range(1000000):
        c = str((n * 7919) % 1000000).zfill(6)
        if not da._totp_ok(secret, c) and da._clock_offset(secret, c) is None:
            return c


def test_a_code_right_for_another_time_says_the_clock_is_out():
    """Running PawPoller on your own computer, its clock is the one codes are checked against. A code that's right a
    few minutes or a few hours (a wrong time zone, the time then set by hand) from that clock is refused, and turning
    2FA on says which way the clocks disagree; a code that's simply wrong says so plainly. Nothing is accepted."""
    import time
    import pyotp
    _configure()
    c = _client()
    secret = c.post("/api/auth/totp-setup").json()["secret"]
    totp = pyotp.TOTP(secret)
    for shift, words in ((600, "about 10 minutes ahead of"), (-3 * 3600, "about 3 hours behind")):
        r = c.post("/api/auth/totp-enable", json={"code": totp.at(time.time() + shift), "password": "pw12345678"})
        assert r.status_code == 400 and words in r.json()["detail"], r.text
    r = c.post("/api/auth/totp-enable", json={"code": _wrong_everywhere(secret), "password": "pw12345678"})
    assert r.status_code == 400 and "clock" not in r.json()["detail"]
    assert not config.get_settings().get("auth_totp_enabled")


def test_signing_in_with_a_code_for_another_time_logs_the_clock_not_the_reply(caplog):
    import logging
    import time
    import pyotp
    _configure()
    c = _client()
    _enable_2fa(c)
    secret = config.get_settings()["auth_totp_secret"]
    with caplog.at_level(logging.WARNING):
        r = c.post("/api/auth/dashboard-login", json={"username": "admin", "password": "pw12345678",
                                                      "totp_code": pyotp.TOTP(secret).at(time.time() - 600)})
    assert r.status_code == 401 and "clock" not in r.text, "the person signing in may not be the owner"
    assert "would be right -10 min from this machine's clock" in caplog.text


def test_a_seven_or_eight_digit_code_from_the_same_key_works():
    """Authy lets an account be saved with 7- or 8-digit codes. A longer code from the same key and moment ends in the
    six-digit one, so it's taken by its last six digits (no easier to guess); its first six never match. And the
    QR code now says six digits, 30 seconds and SHA-1 outright, so an app doesn't have to guess."""
    import time
    import pyotp
    _configure()
    c = _client()
    r = c.post("/api/auth/totp-setup").json()
    assert r["uri"].endswith("&algorithm=SHA1&digits=6&period=30")
    assert pyotp.parse_uri(r["uri"]).digits == 6
    secret = r["secret"]
    seven = pyotp.TOTP(secret, digits=7).now()
    assert c.post("/api/auth/totp-enable", json={"code": seven, "password": "pw12345678"}).status_code == 200
    eight = pyotp.TOTP(secret, digits=8).now()
    ok = c.post("/api/auth/dashboard-login", json={"username": "admin", "password": "pw12345678", "totp_code": eight})
    assert ok.status_code == 200
    wrong7 = "9" + _wrong_everywhere(secret)
    assert c.post("/api/auth/dashboard-login", json={"username": "admin", "password": "pw12345678", "totp_code": wrong7}).status_code == 401
    later = pyotp.TOTP(secret, digits=7).at(time.time() + 600)
    assert c.post("/api/auth/totp-disable", json={"password": "pw12345678", "code": later}).json()["detail"].startswith("That code would be right about 10 minutes ahead")


def test_a_code_in_other_digits_is_refused_and_logged(caplog):
    """Fullwidth or other non-ASCII digits: refused with the usual 401 and the usual log line, never a crash."""
    import logging
    _configure()
    c = _client()
    _enable_2fa(c)
    with caplog.at_level(logging.WARNING):
        r = c.post("/api/auth/dashboard-login", json={"username": "admin", "password": "pw12345678", "totp_code": "０" * 6})
    assert r.status_code == 401 and "bad 2FA code" in caplog.text
