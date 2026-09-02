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
    assert c.post("/api/auth/dashboard-setup", json=body).status_code == 200
    assert config.get_settings().get("auth_password_hash")


def test_api_key_validation_still_works_constant_time():
    _configure()
    c = _client()
    key = c.post("/api/auth/api-keys", json={"name": "test"}).json()["key"]
    assert config.validate_api_key(key) is True
    assert config.validate_api_key("pp_deadbeef00") is False
    assert config.validate_api_key("not-a-key") is False
