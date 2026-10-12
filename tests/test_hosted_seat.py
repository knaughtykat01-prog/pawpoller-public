"""Hosted beta seats on the home box (4.70.0, spec 034): the invite claim, the hosted
disclosure, the backup-keep seed and the poll start offset. All inert without their env."""
from __future__ import annotations

import hashlib
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import config
import legal

ROOT = Path(__file__).resolve().parents[1]
_KEYS = ["auth_username", "auth_password_hash", "auth_email", "auth_email_pending",
         "dashboard_password", "legal_accepted", "legal_history", "auto_backup_keep"]
_CODE = "k3Yv0Q2mXc8pLr7TzW1aB9nHs4dF6gJe"
_GOOD = {"username": "SecondFur", "email": "owner@example.com", "password": "correct horse",
         "confirm": "correct horse"}


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    import dashboard
    for k in ("DASHBOARD_PASSWORD", "PAWPOLLER_ALLOW_OPEN_SETUP", "PAWPOLLER_HOSTED",
              "PAWPOLLER_CLAIM_HASH", "PAWPOLLER_CLAIM_EXPIRES"):
        monkeypatch.delenv(k, raising=False)
    saved = {k: v for k, v in config.get_settings().items() if k in _KEYS}
    config.delete_settings_keys(_KEYS)
    config.invalidate_auth_required_cache()
    dashboard._auth_failures.clear()
    yield
    dashboard._auth_failures.clear()
    config.delete_settings_keys(_KEYS)
    if saved:
        config.save_settings(saved)
    config.invalidate_auth_required_cache()


def _seat(monkeypatch, expires_in=3600):
    monkeypatch.setenv("PAWPOLLER_HOSTED", "1")
    monkeypatch.setenv("PAWPOLLER_CLAIM_HASH", hashlib.sha256(_CODE.encode()).hexdigest())
    monkeypatch.setenv("PAWPOLLER_CLAIM_EXPIRES", str(int(time.time()) + expires_in))


def _remote():
    import dashboard
    return TestClient(dashboard.app)   # host "testclient": not loopback, like a tunnel visitor


def test_remote_setup_without_a_claim_is_still_refused(monkeypatch):
    _seat(monkeypatch)
    r = _remote().post("/api/auth/dashboard-setup", json=_GOOD)
    assert r.status_code == 403 and not config.get_settings().get("auth_password_hash")


def test_the_invite_code_makes_the_account_once(monkeypatch):
    _seat(monkeypatch)
    c = _remote()
    st = c.get("/api/auth/signup-status").json()
    assert st["hosted"] and st["claim"] and not st["account"]
    assert "answered" not in st                       # a stranger still learns nothing else
    assert c.post("/api/auth/dashboard-setup", json={**_GOOD, "claim": _CODE}).status_code == 200
    assert config.get_settings()["auth_username"] == "SecondFur"
    # Single use: the account now exists.
    again = _remote().post("/api/auth/dashboard-setup", json={**_GOOD, "username": "ThirdFur", "claim": _CODE})
    assert again.status_code == 403 and config.get_settings()["auth_username"] == "SecondFur"
    assert not _remote().get("/api/auth/signup-status").json()["claim"]


@pytest.mark.parametrize("code, expires_in", [("wrong-code-wrong-code", 3600), (_CODE, -1)])
def test_a_wrong_or_expired_code_is_refused_plainly(monkeypatch, code, expires_in):
    _seat(monkeypatch, expires_in)
    r = _remote().post("/api/auth/dashboard-setup", json={**_GOOD, "claim": code})
    assert r.status_code == 403 and r.json()["detail"]["error"] == "claim"
    assert "expired or was already used" in r.json()["detail"]["message"]
    assert not config.get_settings().get("auth_password_hash")


def test_wrong_codes_count_as_failed_sign_ins(monkeypatch):
    _seat(monkeypatch)
    import dashboard
    for _ in range(20):
        _remote().post("/api/auth/dashboard-setup", json={**_GOOD, "claim": "guess-guess-guess-1"})
    assert dashboard._is_rate_limited("testclient")
    assert _remote().post("/api/auth/dashboard-setup", json={**_GOOD, "claim": _CODE}).status_code == 429


def test_a_claim_does_nothing_on_an_ordinary_install(monkeypatch):
    """No claim env (desktop, self-hosted): the loopback rule is exactly as before."""
    monkeypatch.setenv("PAWPOLLER_HOSTED", "1")   # even hosted, without a hash nothing opens
    r = _remote().post("/api/auth/dashboard-setup", json={**_GOOD, "claim": _CODE})
    assert r.status_code == 403
    assert not _remote().get("/api/auth/signup-status").json()["claim"]


def test_hosted_adds_a_third_document_to_accept(monkeypatch):
    assert "hosted" not in legal.current()
    monkeypatch.setenv("PAWPOLLER_HOSTED", "1")
    cur = legal.current()
    assert cur["hosted"] >= 1
    with pytest.raises(ValueError):
        legal.accept(cur["terms"], cur["privacy"])          # the hosted text must be agreed too
    rec = legal.accept(cur["terms"], cur["privacy"], cur["hosted"])
    assert rec["hosted"] == cur["hosted"] and not legal.needs_accept()
    # A newer hosted text asks again.
    assert legal.needs_accept({"legal_accepted": {**rec, "hosted": cur["hosted"] - 1}})
    # An agreement made before the seat was hosted doesn't count.
    assert legal.needs_accept({"legal_accepted": {"terms": cur["terms"], "privacy": cur["privacy"]}})


def test_the_hosted_text_says_the_hard_parts():
    html = (ROOT / "frontend" / "legal" / "hosted.html").read_text(encoding="utf-8")
    assert 'data-version="' in html and "<script" not in html and "style=" not in html
    for words in ("someone else's computer", "could in principle reach", "may be paused, moved or wiped",
                  "same computer", "Download everything", "don't move", "never sold"):
        assert words.lower() in html.lower(), words


def test_backup_keep_seeds_from_env(monkeypatch):
    import server
    monkeypatch.setenv("PAWPOLLER_AUTO_BACKUP_KEEP", "14")
    server._seed_settings_from_env()
    assert config.get_settings()["auto_backup_keep"] == 14


@pytest.mark.parametrize("raw, want", [(None, 0), ("12", 12), ("-3", 0), ("75", 59), ("soon", 0)])
def test_poll_offset(monkeypatch, raw, want):
    import server
    if raw is None:
        monkeypatch.delenv("PAWPOLLER_POLL_OFFSET_MINUTES", raising=False)
    else:
        monkeypatch.setenv("PAWPOLLER_POLL_OFFSET_MINUTES", raw)
    assert server._poll_offset_minutes() == want


def test_the_front_end_drops_the_code_from_the_address_bar():
    src = (ROOT / "frontend" / "js" / "app.js").read_text(encoding="utf-8")
    init = src[src.index("async init() {"):src.index("Listen for hash changes")]
    assert "history.replaceState" in init and "this._claim" in init
    assert "This PawPoller is waiting for its owner" in src
