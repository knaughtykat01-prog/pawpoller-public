"""How long "Remember me" lasts, chosen in Settings → Security (4.92.4, STAYSIGNEDIN).

The server enforces the length (the signed cookie's age), not the browser, so changing it needs the password:
otherwise a stolen session could stretch itself to a year. Shortening it signs older sessions out at once.
"""
import itsdangerous.timed
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import config
import routes.dashboard_auth as da

PW = "pw12345678"
DAY = 86400


def _client():
    app = FastAPI()
    app.include_router(da.dashboard_auth_router)
    return TestClient(app)


@pytest.fixture(autouse=True)
def _account():
    config.save_settings({"auth_username": "admin", "auth_password_hash": config.hash_password(PW)})


_REAL_CLOCK = itsdangerous.timed.TimestampSigner.get_timestamp


def _later(monkeypatch, days):
    """The session check runs `days` after now (always from the real clock, so calls don't add up)."""
    monkeypatch.setattr(itsdangerous.timed.TimestampSigner, "get_timestamp",
                        lambda self: _REAL_CLOCK(self) + days * DAY)


def _signed_in(c, remember=True):
    r = c.post("/api/auth/dashboard-login", json={"username": "admin", "password": PW, "remember": remember})
    assert r.status_code == 200, r.text
    return r


def test_it_is_thirty_days_until_chosen():
    c = _client()
    assert config.remember_days() == 30
    assert c.get("/api/auth/dashboard-status").json()["remember_days"] == 30
    assert "Max-Age=2592000" in _signed_in(c).headers["set-cookie"]


def test_a_longer_choice_keeps_a_remembered_sign_in_alive(monkeypatch):
    c = _client()
    _signed_in(c)
    assert c.post("/api/auth/remember-days", json={"days": 365, "password": PW}).status_code == 200
    cookie = c.cookies["pp_session"]                          # the change re-issued this session's cookie
    _later(monkeypatch, 200)
    assert config.verify_session(cookie) is not None          # 200 days on, still signed in
    _later(monkeypatch, 366)
    assert config.verify_session(cookie) is None              # but not past a year


def test_a_new_sign_in_gets_a_cookie_that_long():
    c = _client()
    c.post("/api/auth/remember-days", json={"days": 90, "password": PW})
    assert "Max-Age=7776000" in _signed_in(c).headers["set-cookie"]
    assert c.get("/api/auth/dashboard-status").json()["remember_days"] == 90


def test_the_session_making_the_change_gets_the_new_length_now():
    c = _client()
    _signed_in(c)
    r = c.post("/api/auth/remember-days", json={"days": 365, "password": PW})
    assert r.status_code == 200 and "Max-Age=31536000" in r.headers["set-cookie"]


def test_raising_it_does_not_bring_back_a_sign_in_that_had_run_out(monkeypatch):
    """4.92.4's release review (Low): the cookie carries its own length, so a stolen copy of one signed under
    30 days stays dead at day 31 even after the owner picks a year."""
    c = _client()
    cookie = _signed_in(c).cookies["pp_session"]
    config.set_remember_days(365)
    _later(monkeypatch, 31)
    assert config.verify_session(cookie) is None


def test_a_remembered_cookie_from_before_this_version_still_lasts_thirty_days(monkeypatch):
    old = config.sign_session({"u": "admin", "r": True})
    config.set_remember_days(365)
    _later(monkeypatch, 29)
    assert config.verify_session(old) is not None
    _later(monkeypatch, 31)
    assert config.verify_session(old) is None


def test_a_wrong_password_counts_toward_the_sign_in_lockout(monkeypatch):
    import dashboard
    hits = []
    monkeypatch.setattr(dashboard, "_record_auth_failure", lambda host: hits.append(host))
    assert _client().post("/api/auth/remember-days", json={"days": 90, "password": "nope"}).status_code == 401
    assert len(hits) == 1


def test_a_sign_in_without_remember_me_is_still_a_day(monkeypatch):
    c = _client()
    config.set_remember_days(365)
    cookie = _signed_in(c, remember=False).cookies["pp_session"]
    _later(monkeypatch, 2)
    assert config.verify_session(cookie) is None


def test_shortening_it_signs_older_sessions_out_at_once(monkeypatch):
    c = _client()
    config.set_remember_days(365)
    cookie = _signed_in(c).cookies["pp_session"]
    _later(monkeypatch, 60)
    assert config.verify_session(cookie) is not None
    config.set_remember_days(30)
    assert config.verify_session(cookie) is None


def test_changing_it_needs_the_password_and_a_listed_length():
    c = _client()
    for body in ({"days": 365}, {"days": 365, "password": "nope"}):
        assert c.post("/api/auth/remember-days", json=body).status_code == 401
    for days in (7, 3650, "365", 365.0, True, None):
        assert c.post("/api/auth/remember-days", json={"days": days, "password": PW}).status_code == 400, days
    assert config.remember_days() == 30


def test_a_hand_edited_value_off_the_list_reads_as_thirty():
    for bad in (3650, "365", 0, -1, True, 90.0):
        config.save_settings({"auth_remember_days": bad})
        assert config.remember_days() == 30, bad


def test_saving_preferences_cannot_set_it():
    from routes.api import save_preferences
    save_preferences({"auth_remember_days": 365})
    assert config.remember_days() == 30


def test_it_needs_a_session_like_every_other_account_change():
    import dashboard
    assert "/api/auth/remember-days" not in dashboard._AUTH_EXEMPT_PATHS
