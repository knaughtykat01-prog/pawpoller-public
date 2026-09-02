"""Vault always-on (2.101.0): plaintext credential storage no longer exists.

- save_settings() routes secrets to the vault unconditionally (no mode key).
- ensure_vault() sweeps plaintext stragglers (pre-2.101.0 files, hand edits,
  old-backup restores) into the vault at startup.
- Deleting the LAST credential rewrites the vault — a stale ciphertext must
  not resurrect the deleted secret on the next load (pre-2.101.0 bug: the
  vault was only rewritten when credentials remained).
"""
import json

import config


def _raw_settings() -> dict:
    return json.loads(config.SETTINGS_PATH.read_text(encoding="utf-8"))


def test_save_settings_routes_secret_to_vault_without_mode_key():
    # Fresh install: no credential_mode anywhere — secrets must still be vaulted.
    config.save_settings({"tw_auth_token": "sekrit-token", "theme": "dark"})
    raw = _raw_settings()
    assert "tw_auth_token" not in raw
    assert raw["theme"] == "dark"
    assert raw["credential_mode"] == "local"  # stamped for downgrade compat
    assert config.VAULT_PATH.exists()
    assert config.get_settings()["tw_auth_token"] == "sekrit-token"


def test_ensure_vault_sweeps_plaintext_stragglers():
    # Simulate a pre-2.101.0 settings.json: plaintext secret, no mode key.
    config.SETTINGS_PATH.write_text(
        json.dumps({"ao3_password": "hunter2", "theme": "dark"}), encoding="utf-8")
    migrated = config.ensure_vault()
    assert migrated == 1
    raw = _raw_settings()
    assert "ao3_password" not in raw
    assert raw["credential_mode"] == "local"
    assert config.get_settings()["ao3_password"] == "hunter2"
    # Idempotent: a clean file is a no-op.
    assert config.ensure_vault() == 0


def test_deleting_last_credential_does_not_resurrect():
    config.save_settings({"fa_cookie_a": "aaa"})
    assert config.get_settings()["fa_cookie_a"] == "aaa"
    config.delete_settings_keys(["fa_cookie_a"])
    # The vault must have been rewritten (empty), not left stale.
    assert "fa_cookie_a" not in config.get_settings()
    config.save_settings({"theme": "light"})  # any later save
    assert "fa_cookie_a" not in config.get_settings()


def test_vault_key_source_reports_operator_under_suite_env():
    # conftest supplies PAWPOLLER_VAULT_KEY for the whole suite.
    assert config.vault_key_source() == "operator"


def test_get_credential_mode_is_always_local():
    assert config.get_credential_mode() == "local"
