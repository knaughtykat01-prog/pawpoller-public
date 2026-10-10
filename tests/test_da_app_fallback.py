"""An extra DeviantArt account with no app of its own uses the default app (DAAPPFALLBACK).

Found during the brand launch (2026-10-07): a second DA account authorised
fine, because the authorise route fell back to the flat da_client_id /
da_client_secret, but its poster then said "DeviantArt OAuth not configured"
because the poster read only the account's own keys. The fallback now lives in
the one resolver everything shares, so the route, the poster and the poller
see the same app.
"""
from __future__ import annotations

import config

_FLAT = {"da_client_id": "app-id", "da_client_secret": "app-secret",
         "da_refresh_token": "default-refresh"}


def test_an_extra_account_without_an_app_uses_the_default_one():
    settings = dict(_FLAT, acct_7_da_refresh_token="second-refresh")
    creds = config.resolve_account_credentials("da", 7, False, settings)
    assert creds["da_client_id"] == "app-id"
    assert creds["da_client_secret"] == "app-secret"
    # The account's own token, never the default account's.
    assert creds["da_refresh_token"] == "second-refresh"


def test_an_extra_account_with_its_own_app_keeps_it():
    settings = dict(_FLAT, acct_7_da_client_id="own-id",
                    acct_7_da_client_secret="own-secret")
    creds = config.resolve_account_credentials("da", 7, False, settings)
    assert (creds["da_client_id"], creds["da_client_secret"]) == ("own-id", "own-secret")


def test_a_missing_token_is_not_borrowed_from_the_default_account():
    creds = config.resolve_account_credentials("da", 7, False, dict(_FLAT))
    assert creds["da_refresh_token"] == ""


def test_other_platforms_do_not_fall_back():
    settings = {"bsky_identifier": "me.bsky.social", "bsky_app_password": "pw"}
    creds = config.resolve_account_credentials("bsky", 7, False, settings)
    assert creds["bsky_identifier"] == "" and creds["bsky_app_password"] == ""


def test_the_poster_and_the_route_resolve_the_same_app(monkeypatch):
    from posting.platforms.deviantart import DeviantArtPoster
    from routes import da_api

    settings = dict(_FLAT, acct_7_da_refresh_token="second-refresh")
    monkeypatch.setattr(config, "get_settings", lambda: settings)

    class _Acct(dict):
        pass

    from database import accounts as _accts
    monkeypatch.setattr(_accts, "get_account",
                        lambda conn, aid: _Acct(id=aid, is_default=0, platform="da"))
    monkeypatch.setattr(da_api, "get_connection", lambda: type("C", (), {"close": lambda s: None})())

    route_app = da_api._da_app_creds(7)

    poster = DeviantArtPoster()
    poster.account_id = 7
    import database.db as _db
    monkeypatch.setattr(_db, "get_connection", lambda: type("C", (), {"close": lambda s: None})())
    creds = poster._resolve_creds("da", settings)

    assert route_app == ("app-id", "app-secret")
    assert (creds["da_client_id"], creds["da_client_secret"]) == route_app
