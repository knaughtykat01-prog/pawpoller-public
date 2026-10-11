"""First sign-up before the wizard (4.67.0, spec 033 US1 + US3 + the gates)."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import config

ROOT = Path(__file__).resolve().parents[1]
_KEYS = ["auth_username", "auth_password_hash", "auth_email", "auth_email_pending",
         "dashboard_password", "legal_accepted", "legal_history"]
_LOCAL = ("127.0.0.1", 50000)


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    saved = {k: v for k, v in config.get_settings().items() if k in _KEYS}
    config.delete_settings_keys(_KEYS)
    config.invalidate_auth_required_cache()
    yield
    config.delete_settings_keys(_KEYS)
    if saved:
        config.save_settings(saved)
    config.invalidate_auth_required_cache()


def _client(local=True):
    import dashboard
    return TestClient(dashboard.app, client=_LOCAL) if local else TestClient(dashboard.app)


_GOOD = {"username": "SecondFur", "email": "Owner@Example.com", "password": "correct horse",
         "confirm": "correct horse", "remember": True}


@pytest.mark.parametrize("change, field", [
    ({"email": ""}, "email"), ({"email": "not-an-email"}, "email"),
    ({"username": ""}, "username"), ({"username": "a b"}, "username"),
    ({"password": "short", "confirm": "short"}, "password"), ({"confirm": "different"}, "confirm"),
])
def test_setup_names_the_field(change, field):
    r = _client().post("/api/auth/dashboard-setup", json={**_GOOD, **change})
    assert r.status_code == 400 and r.json()["detail"]["field"] == field
    assert not config.get_settings().get("auth_password_hash")


def test_setup_creates_the_account_and_signs_in():
    c = _client()
    r = c.post("/api/auth/dashboard-setup", json=_GOOD)
    assert r.status_code == 200
    s = config.get_settings()
    assert s["auth_username"] == "SecondFur" and s["auth_email_pending"] == "owner@example.com"
    assert "Max-Age=2592000" in r.headers["set-cookie"]   # keep me signed in = 30 days
    st = c.get("/api/auth/signup-status").json()
    assert st["account"] and st["authenticated"] and st["email"] == "waiting"
    # Never twice.
    assert c.post("/api/auth/dashboard-setup", json=_GOOD).status_code == 403


def test_no_admin_default():
    r = _client().post("/api/auth/dashboard-setup", json={k: v for k, v in _GOOD.items() if k != "username"})
    assert r.status_code == 400


def test_signup_status_signed_out_says_little():
    config.save_settings({"auth_password_hash": config.hash_password("x" * 8), "auth_username": "SecondFur"})
    config.invalidate_auth_required_cache()
    st = _client().get("/api/auth/signup-status").json()
    assert st["account"] and not st["authenticated"]
    assert "answered" not in st and "email" not in st


def test_answered_follows_the_settings():
    config.save_settings({"age_band": "adult", "display_timezone": "UTC"})
    config.delete_settings_keys(["tech_usage"])
    st = _client().get("/api/auth/signup-status").json()
    assert st["answered"]["age"] and st["answered"]["timezone"] and not st["answered"]["tech"]


def test_add_email_needs_the_current_password():
    c = _client()
    c.post("/api/auth/dashboard-setup", json=_GOOD)
    import dashboard
    import legal
    legal.accept(**{"terms": legal.current()["terms"], "privacy": legal.current()["privacy"]})
    r = c.post("/api/auth/email", json={"email": "new@example.com", "current_password": "wrong one"})
    assert r.status_code == 401 and r.json()["detail"]["field"] == "current"
    r = c.post("/api/auth/email", json={"email": "new@example.com", "current_password": "correct horse"})
    assert r.status_code == 200 and config.get_settings()["auth_email_pending"] == "new@example.com"
    assert dashboard  # imported for the app


def test_remote_caller_meets_the_signup_wall(monkeypatch):
    import dashboard
    monkeypatch.setattr(dashboard, "SIGNUP_GATES", True)
    remote = _client(local=False)
    r = remote.get("/api/settings/preferences")
    assert r.status_code == 403 and r.json()["error"] == "signup"
    assert remote.get("/api/auth/signup-status").status_code == 200
    assert remote.get("/legal/terms.html").status_code == 200
    # The person at the machine still gets through (the SPA sends them to sign-up).
    assert _client().get("/api/settings/preferences").status_code == 200


def test_gates_are_off_unless_an_entry_point_turns_them_on():
    import dashboard
    assert dashboard.SIGNUP_GATES is False
    for entry in ("main.py", "server.py"):
        assert "dashboard.SIGNUP_GATES = True" in (ROOT / entry).read_text(encoding="utf-8"), entry


def test_wizard_skips_what_signup_asked():
    src = (ROOT / "frontend" / "js" / "app.js").read_text(encoding="utf-8")
    wiz = src[src.index("async renderSetupWizard("):src.index("renderLogin() {")] \
        if "async renderSetupWizard(" in src else src[src.index("renderSetupWizard("):src.index("renderLogin() {")]
    assert "&& !answered[s]" in wiz
    assert "['welcome', 'timezone', 'age', 'mode', 'pairing', 'hear', 'tech', 'done'].filter(keep)" in wiz


def test_beacons_start_off_in_signup():
    src = (ROOT / "frontend" / "js" / "app.js").read_text(encoding="utf-8")
    tech = src[src.index("// screen === 'tech'"):src.index("/* ── Setup Wizard")]
    assert 'id="signup-reports">' in tech and 'id="signup-usage">' in tech   # no `checked`
    assert "API.setTechConsent(reports)" in tech and "API.setTechUsage(usage)" in tech
