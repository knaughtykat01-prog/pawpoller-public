"""Per-account credential re-entry and login testing (3.20.0).

The operator, immediately after pasting renewed FA cookies into the wrong account:
*"it would be nice to have a handy reinput for the cookies on accounts, or
anything really for any account for a platform that has something that
expires."*

The mistake was structural, not careless. The accounts UI could CREATE an
account with credentials and rename it, but had **no way to update an existing
account's credentials** — so the only visible place to paste a renewed cookie
was the main per-platform form, which writes the DEFAULT account's keys.
Renewing account 15's cookies there looked like it worked and changed nothing
for account 15.

Two additions:

  * every account row gets a credentials editor (backend already supported it
    via `AccountUpdate.credentials`; the front door was missing);
  * `POST /api/accounts/{id}/test-login` checks whether ONE account's stored
    login still works — the platform-level session checks could say "FA
    session expired" but never *which account*.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import config
from database import accounts as adb
from database.db import get_connection


@pytest.fixture()
def client():
    from dashboard import app
    return TestClient(app)


@pytest.fixture()
def fa_account():
    conn = get_connection()
    try:
        aid = adb.create_account(conn, "fa", "KiTest", handle="KiTest")
    finally:
        conn.close()
    return aid


# ── the endpoint ─────────────────────────────────────────────────

def test_missing_cookies_report_unconfigured(client, fa_account):
    r = client.post(f"/api/accounts/{fa_account}/test-login")
    assert r.status_code == 200
    assert r.json()["status"] == "unconfigured"


def test_a_dead_session_reports_invalid_and_says_where_to_paste(
        client, fa_account, monkeypatch):
    import clients.fa.client as fac

    # The endpoint moved to validate_session in 3.31.0 — it needs to know WHO
    # a session belongs to, not only that one exists.
    async def _invalid(self):
        return {"ok": False, "logged_in": False, "username": "",
                "expected": self.username, "matches": False, "detail": ""}
    monkeypatch.setattr(fac.FAClient, "validate_session", _invalid)
    config.save_settings({f"acct_{fa_account}_fa_cookie_a": "dead",
                          f"acct_{fa_account}_fa_cookie_b": "dead",
                          f"acct_{fa_account}_fa_username": "KiTest"})
    body = client.post(f"/api/accounts/{fa_account}/test-login").json()
    assert body["status"] == "invalid"
    assert "THIS account" in body["detail"]


def test_a_live_session_reports_ok(client, fa_account, monkeypatch):
    import clients.fa.client as fac

    async def _valid(self):
        return {"ok": True, "logged_in": True, "username": self.username,
                "expected": self.username, "matches": True, "detail": ""}
    monkeypatch.setattr(fac.FAClient, "validate_session", _valid)
    config.save_settings({f"acct_{fa_account}_fa_cookie_a": "fresh",
                          f"acct_{fa_account}_fa_cookie_b": "fresh"})
    assert client.post(f"/api/accounts/{fa_account}/test-login").json()["status"] == "ok"


def test_it_tests_the_named_accounts_credentials_not_the_defaults(
        client, fa_account, monkeypatch):
    """The heart of the original mistake: the default account's cookies were
    fresh while account 15's were dead. The test must resolve the NAMED
    account's keys, or it would report the wrong account's health."""
    import clients.fa.client as fac
    seen = {}

    async def _capture(self):
        seen["cookie_a"] = self.cookie_a
        return {"ok": True, "logged_in": True, "username": self.username,
                "expected": self.username, "matches": True, "detail": ""}
    monkeypatch.setattr(fac.FAClient, "validate_session", _capture)
    config.save_settings({
        "fa_cookie_a": "default_accounts_cookie", "fa_cookie_b": "x",
        f"acct_{fa_account}_fa_cookie_a": "this_accounts_cookie",
        f"acct_{fa_account}_fa_cookie_b": "y",
    })
    client.post(f"/api/accounts/{fa_account}/test-login")
    assert seen["cookie_a"] == "this_accounts_cookie"


def test_an_unsupported_platform_says_so_rather_than_guessing(client):
    conn = get_connection()
    try:
        aid = adb.create_account(conn, "ib", "IbTest", handle="IbTest")
    finally:
        conn.close()
    body = client.post(f"/api/accounts/{aid}/test-login").json()
    assert body["status"] == "unsupported"


def test_an_unknown_account_is_a_404(client):
    assert client.post("/api/accounts/99999/test-login").status_code == 404


# ── credentials update through the accounts API ──────────────────

def test_updating_credentials_writes_the_accounts_own_keys(client, fa_account):
    """The fix for the original mistake, end to end: a PATCH on the account
    must land on `acct_<id>_*`, never on the bare default keys."""
    r = client.patch(f"/api/accounts/{fa_account}",
                     json={"credentials": {"fa_cookie_a": "renewed_a"}})
    assert r.status_code == 200
    s = config.get_settings()
    assert s.get(f"acct_{fa_account}_fa_cookie_a") == "renewed_a"
    assert s.get("fa_cookie_a") != "renewed_a", "must not touch the default account"


def test_a_partial_update_leaves_other_fields_alone(client, fa_account):
    """Empty means unchanged: renewing one cookie must not blank the rest."""
    config.save_settings({f"acct_{fa_account}_fa_cookie_b": "keep_me"})
    client.patch(f"/api/accounts/{fa_account}",
                 json={"credentials": {"fa_cookie_a": "only_this"}})
    assert config.get_settings().get(f"acct_{fa_account}_fa_cookie_b") == "keep_me"


# ── the UI has the front door ────────────────────────────────────

def _js():
    from pathlib import Path
    return (Path(__file__).resolve().parent.parent / "frontend" / "js" /
            "accounts.js").read_text(encoding="utf-8", errors="replace")


def test_every_account_row_offers_credential_reentry():
    src = _js()
    assert "data-creds" in src and "_editCredentials" in src


def test_every_account_row_offers_a_login_test():
    src = _js()
    assert "data-test-login" in src and "testAccountLogin" in src


def test_every_api_helper_the_accounts_page_calls_is_defined():
    """⚠ Caught live, the embarrassing way. `accounts.js` called
    `API.testAccountLogin(...)` and `api.js` never defined it — the edit that
    was supposed to add it died on a syntax error in the tooling and only the
    calling half was retried. The original test asserted the string
    "testAccountLogin" appeared in accounts.js, which the CALL satisfies — so
    it passed while the user got "API.testAccountLogin is not a function".

    A call site is not evidence of a definition. This pins every `API.x(`
    the accounts page makes against an `x(` definition in api.js — the same
    guard the Sync panel got after the identical mistake with `this._toast`.
    """
    import re
    from pathlib import Path
    api_src = (Path(__file__).resolve().parent.parent / "frontend" / "js" /
               "api.js").read_text(encoding="utf-8", errors="replace")
    acc_src = _js()
    called = set(re.findall(r"API\.(\w+)\s*\(", acc_src))
    defined = set(re.findall(r"^\s{4}(?:async\s+)?(\w+)\s*\(", api_src, re.MULTILINE))
    missing = sorted(called - defined)
    assert missing == [], f"accounts.js calls API methods api.js never defines: {missing}"


def test_the_editor_treats_empty_as_unchanged():
    """Prefilling secrets is out (write-only), so empty-means-unchanged is what
    makes renewing a single cookie practical."""
    src = _js()
    assert 'placeholder="unchanged"' in src
    assert "if (inp.value) credentials[inp.dataset.credField]" in src
