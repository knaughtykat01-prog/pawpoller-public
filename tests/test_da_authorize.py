"""Tests for the DeviantArt posting authorisation (3.9.1).

Polling and posting need different tokens from the same app, and only polling
had a route. So posting failed with "DeviantArt OAuth not configured" while
Settings showed DeviantArt connected — because it was, for polling. These tests
pin the second half.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import config
from routes import da_api


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(config, "VAULT_PATH", tmp_path / "settings.vault.json")
    config.SETTINGS_PATH.write_text("{}", encoding="utf-8")
    app = FastAPI()
    app.include_router(da_api.da_router)
    da_api._da_oauth_state.clear()
    return TestClient(app)


def test_authorize_is_refused_before_the_app_is_connected(client):
    """No client_id means there is nothing to authorise against, and sending the
    user to DeviantArt would land them on an error page there instead of here."""
    r = client.get("/api/da/auth/authorize-url")
    assert r.status_code == 400
    assert "client_id" in r.json()["detail"]


def test_authorize_url_asks_for_a_code_and_only_the_scopes_posting_needs(client):
    config.save_settings({"da_client_id": "75305", "da_client_secret": "s"})
    body = client.get("/api/da/auth/authorize-url").json()

    assert body["url"].startswith("https://www.deviantart.com/oauth2/authorize?")
    assert "response_type=code" in body["url"]
    assert "client_id=75305" in body["url"]
    assert body["scopes"] == "browse user stash publish"
    # `stash` is not optional: an image is uploaded to Sta.sh and published from
    # there, and leaving it out earned a 403 insufficient_scope on prod naming
    # exactly this pair — "scope":"stash publish".
    for needed in ("publish", "stash"):
        assert needed in body["url"], needed
    # Everything beyond what posting uses is still a permission for nothing.
    for unwanted in ("collection", "message", "note"):
        assert unwanted not in body["url"]


def test_the_redirect_uri_follows_the_browsers_view_not_the_apps(client):
    """Behind Caddy + Cloudflare the app sees plain http on an internal port
    while the browser used https on the public host. Getting this wrong gives
    DA's "redirect_uri mismatch", which reads like a whitelist problem."""
    config.save_settings({"da_client_id": "75305", "da_client_secret": "s"})
    body = client.get("/api/da/auth/authorize-url", headers={
        "x-forwarded-proto": "https",
        "x-forwarded-host": "pawpoller.syncopates.app",
    }).json()
    assert body["redirect_uri"] == "https://pawpoller.syncopates.app/api/da/auth/callback"


def test_the_authorize_url_carries_a_pkce_challenge(client):
    """DeviantArt REQUIRES PKCE. Without code_challenge the authorize call is
    refused outright — observed live on 2026-08-19:
    `invalid_request: The code_challenge parameter is required.`
    """
    config.save_settings({"da_client_id": "75305", "da_client_secret": "s"})
    url = client.get("/api/da/auth/authorize-url").json()["url"]
    assert "code_challenge=" in url
    assert "code_challenge_method=S256" in url


def test_the_verifier_is_kept_here_and_never_sent_to_the_browser(client):
    """The whole point of PKCE: only the hash travels, so an intercepted code
    cannot be redeemed by whoever intercepted it."""
    config.save_settings({"da_client_id": "75305", "da_client_secret": "s"})
    body = client.get("/api/da/auth/authorize-url").json()
    state = list(da_api._da_oauth_state)[0]
    verifier = da_api._da_oauth_state[state]["verifier"]
    assert verifier
    assert verifier not in body["url"]


def test_the_challenge_is_the_unpadded_base64url_sha256_of_the_verifier(client):
    """RFC 7636's exact construction. Sending the `=` padding is a standard way
    to earn an opaque invalid_grant at exchange time."""
    import base64
    import hashlib
    import urllib.parse

    config.save_settings({"da_client_id": "75305", "da_client_secret": "s"})
    url = client.get("/api/da/auth/authorize-url").json()["url"]
    state = list(da_api._da_oauth_state)[0]
    verifier = da_api._da_oauth_state[state]["verifier"]

    sent = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["code_challenge"][0]
    expected = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()).decode("ascii").rstrip("=")
    assert sent == expected
    assert "=" not in sent
    assert 43 <= len(verifier) <= 128


def test_the_redirect_uri_is_returned_so_it_can_be_whitelisted(client):
    """DA only redirects to a registered URI, and the value depends on how this
    install is reached — so the UI has to be able to show it."""
    config.save_settings({"da_client_id": "75305", "da_client_secret": "s"})
    body = client.get("/api/da/auth/authorize-url").json()
    assert body["redirect_uri"].endswith("/api/da/auth/callback")


def test_a_callback_with_an_unknown_state_is_refused(client):
    """Without this, a link crafted elsewhere could plant someone else's
    authorisation code here and bind posting to their account."""
    config.save_settings({"da_client_id": "75305", "da_client_secret": "s"})
    r = client.get("/api/da/auth/callback", params={"code": "abc", "state": "not-ours"})
    assert r.status_code == 400
    assert "did not come from this install" in r.text
    assert not config.get_settings().get("da_refresh_token")


def test_a_state_is_single_use(client):
    """A replayed callback must not re-run the exchange."""
    config.save_settings({"da_client_id": "75305", "da_client_secret": "s"})
    state = list(_issue_state(client))[0]
    da_api._da_oauth_state.pop(state)  # simulate: already consumed
    r = client.get("/api/da/auth/callback", params={"code": "abc", "state": state})
    assert r.status_code == 400


def test_a_refusal_from_deviantart_is_shown_not_swallowed(client):
    r = client.get("/api/da/auth/callback",
                   params={"error": "access_denied", "error_description": "User said no"})
    assert r.status_code == 400
    assert "User said no" in r.text


def test_a_callback_with_no_code_fails_cleanly(client):
    r = client.get("/api/da/auth/callback")
    assert r.status_code == 400
    assert not config.get_settings().get("da_refresh_token")


def test_posting_status_is_reported_separately_from_polling(client):
    """The two halves fail independently, and /auth/status only ever described
    polling — which is how posting stayed broken while DA showed connected."""
    config.save_settings({"da_client_id": "75305", "da_client_secret": "s",
                          "da_target_user": "secondfur"})
    body = client.get("/api/da/auth/posting-status").json()
    assert body["has_app"] is True
    assert body["has_refresh_token"] is False

    config.save_settings({"da_refresh_token": "rt"})
    assert client.get("/api/da/auth/posting-status").json()["has_refresh_token"] is True


def test_disconnecting_polling_leaves_posting_authorised(client):
    """Disconnect already keeps client_id/secret for the poster; the refresh
    token belongs to the same half and must survive with them."""
    config.save_settings({"da_client_id": "75305", "da_client_secret": "s",
                          "da_target_user": "secondfur", "da_refresh_token": "rt"})
    client.post("/api/da/auth/disconnect")
    assert config.get_settings().get("da_refresh_token") == "rt"


def _issue_state(client):
    client.get("/api/da/auth/authorize-url")
    return set(da_api._da_oauth_state)


# ── Per-account authorisation (3.9.3) ─────────────────────────

def _make_account(platform="da", handle="second", default=False):
    from database.db import get_connection
    conn = get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO accounts (platform, label, handle, enabled, is_default, sort_order) "
            "VALUES (?, ?, ?, 1, ?, 0)", (platform, handle, handle, 1 if default else 0))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


@pytest.fixture
def db(tmp_path, monkeypatch):
    from database import db as db_mod
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "pawpoller.db")
    db_mod.init_db()
    return db_mod


def test_the_default_account_owns_the_bare_key(db, client):
    """It holds the legacy flat credentials and the pre-multi-account history."""
    assert da_api._da_token_key(None) == "da_refresh_token"


def test_a_second_account_gets_its_own_namespaced_key(db, client):
    """A token authorises posting AS one account. Writing the bare key for a
    non-default account would hand its token to the default one."""
    aid = _make_account()
    assert da_api._da_token_key(aid) == f"acct_{aid}_da_refresh_token"


def test_an_unknown_account_is_refused_rather_than_defaulted(db, client):
    with pytest.raises(Exception):
        da_api._da_token_key(999999)


def test_authorising_a_second_account_carries_its_key_through(db, client):
    """Resolved when the link is minted, not at the callback — the operator
    should hear about a bad account before approving on DeviantArt."""
    config.save_settings({"da_client_id": "75305", "da_client_secret": "s"})
    aid = _make_account()
    body = client.get("/api/da/auth/authorize-url", params={"account_id": aid}).json()
    assert body["account_id"] == aid
    assert body["token_key"] == f"acct_{aid}_da_refresh_token"
    state = list(da_api._da_oauth_state)[0]
    assert da_api._da_oauth_state[state]["token_key"] == f"acct_{aid}_da_refresh_token"


def test_posting_status_is_per_account(db, client):
    config.save_settings({"da_client_id": "75305", "da_client_secret": "s"})
    aid = _make_account()

    assert client.get("/api/da/auth/posting-status").json()["has_refresh_token"] is False
    assert client.get("/api/da/auth/posting-status",
                      params={"account_id": aid}).json()["has_refresh_token"] is False

    # Authorising the default must not make the second account look authorised.
    config.save_settings({"da_refresh_token": "rt-default"})
    assert client.get("/api/da/auth/posting-status").json()["has_refresh_token"] is True
    assert client.get("/api/da/auth/posting-status",
                      params={"account_id": aid}).json()["has_refresh_token"] is False

    config.save_settings({f"acct_{aid}_da_refresh_token": "rt-second"})
    assert client.get("/api/da/auth/posting-status",
                      params={"account_id": aid}).json()["has_refresh_token"] is True


def test_a_second_account_falls_back_to_the_shared_app_credentials(db, client):
    """Several DA accounts may share one registered app, or have one each.
    Falling back covers both without making the operator declare which."""
    config.save_settings({"da_client_id": "75305", "da_client_secret": "s"})
    aid = _make_account()
    assert da_api._da_app_creds(aid) == ("75305", "s")


def test_the_stash_scope_is_requested(client):
    """Regression for 3.9.1's under-scoping.

    The image path is upload-to-Sta.sh then publish-from-Sta.sh
    (`oauth_stash_submit` / `oauth_stash_publish`), so `publish` alone is not
    enough. Prod answered with the pair it wanted:

        403 insufficient_scope — "scope":"stash publish"

    Leaving `stash` out looked like good hygiene and was a broken poster.
    """
    config.save_settings({"da_client_id": "75305", "da_client_secret": "s"})
    body = client.get("/api/da/auth/authorize-url").json()
    assert "stash" in body["scopes"].split()


def test_the_route_and_the_cli_ask_for_the_same_scopes():
    """A token minted by the fallback script must be as capable as one minted by
    the UI, or posting works depending on which was used."""
    import re
    from pathlib import Path

    src = Path("scripts/da_authorize.py").read_text(encoding="utf-8")
    m = re.search(r'^SCOPES = "([^"]+)"', src, re.M)
    assert m, "could not find SCOPES in scripts/da_authorize.py"
    assert set(m.group(1).split()) == set(da_api._DA_SCOPES.split())
