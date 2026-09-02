"""SoFurry credential migration (3.4.0).

`config.migrate_sofurry_credentials()` DELETES keys from the settings vault, so it
gets pinned coverage: a matcher that is too greedy would destroy a live token, and
one that is too narrow would leave a real password and session cookie sitting in
the vault for a credential nothing can spend any more.

The per-account key shape is the subtle part — accounts are namespaced
`acct_<N>_<field>` (config.account_setting_key), so the migration matches on a
`_<field>` suffix rather than an exact key.
"""

import config
import pytest


@pytest.fixture
def captured(monkeypatch):
    """Run the migration against an in-memory vault; record what it deletes."""
    state = {"settings": {}, "deleted": []}

    monkeypatch.setattr(config, "get_settings", lambda: dict(state["settings"]))

    def fake_delete(keys):
        state["deleted"].extend(keys)
        for k in keys:
            state["settings"].pop(k, None)

    monkeypatch.setattr(config, "delete_settings_keys", fake_delete)
    return state


def test_deletes_every_legacy_login_field(captured):
    captured["settings"] = {
        "sf_username": "someone@example.com",
        "sf_password": "hunter2",
        "sf_totp_code": "123456",
        "sf_session_cookies": {"cookies": {}},
    }
    config.migrate_sofurry_credentials()
    assert sorted(captured["deleted"]) == [
        "sf_password", "sf_session_cookies", "sf_totp_code", "sf_username",
    ]
    assert captured["settings"] == {}


def test_spares_the_live_token_and_handle(captured):
    """The whole point of the migration is defeated if it eats the new keys."""
    captured["settings"] = {
        "sf_api_token": "a-real-token",
        "sf_display_name": "SomeHandle",
        "sf_password": "hunter2",
    }
    config.migrate_sofurry_credentials()
    assert captured["deleted"] == ["sf_password"]
    assert captured["settings"] == {
        "sf_api_token": "a-real-token", "sf_display_name": "SomeHandle"}


def test_catches_per_account_namespaced_keys(captured):
    """Accounts are keyed acct_<N>_<field>; a plain-key match would miss them."""
    captured["settings"] = {
        "acct_2_sf_password": "b",
        "acct_3_sf_session_cookies": "c",
        "acct_2_sf_api_token": "keep-me",
    }
    config.migrate_sofurry_credentials()
    assert sorted(captured["deleted"]) == [
        "acct_2_sf_password", "acct_3_sf_session_cookies"]
    assert captured["settings"] == {"acct_2_sf_api_token": "keep-me"}


def test_does_not_touch_other_platforms(captured):
    """`sf_` is a short prefix — nothing else may be caught in the blast radius."""
    others = {
        "sqw_username": "x", "sqw_password": "y",
        "ao3_password": "z", "ws_api_key": "k",
        "fa_cookie_a": "c", "username": "u", "password": "p",
    }
    captured["settings"] = dict(others)
    config.migrate_sofurry_credentials()
    assert captured["deleted"] == []
    assert captured["settings"] == others


def test_is_idempotent(captured):
    captured["settings"] = {"sf_password": "hunter2"}
    config.migrate_sofurry_credentials()
    first = list(captured["deleted"])
    config.migrate_sofurry_credentials()
    assert captured["deleted"] == first, "second run must be a no-op"


def test_no_op_on_a_clean_vault(captured):
    captured["settings"] = {"sf_api_token": "t"}
    config.migrate_sofurry_credentials()
    assert captured["deleted"] == []
