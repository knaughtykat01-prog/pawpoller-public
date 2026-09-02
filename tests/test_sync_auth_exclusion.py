"""Instance-access credentials must never cross the desktop↔server sync.

The bug this pins, observed live 2026-08-12: a paired desktop pushed its
settings and the push **replaced the server's `auth_api_keys`** with the
desktop's stale copy — silently revoking the very key the sync was
authenticating with. Every later request 401'd while the UI still reported
"nothing newer to pull".

`auth_session_secret` was already excluded, so the session *secret* had been
recognised as per-device; the credentials it protects had not. Platform
credentials (FA cookies, API tokens) deliberately DO sync — that is the point
of pairing. What must not sync is who can log in to this instance.
"""
from __future__ import annotations

import pytest

import config


ACCESS_KEYS = [
    "auth_api_keys",
    "auth_password_hash",
    "auth_username",
    "auth_session_secret",
    "auth_2fa_secret",
    "auth_2fa_enabled",
    "auth_backup_codes",
]


@pytest.mark.parametrize("key", ACCESS_KEYS)
def test_access_credentials_are_sync_excluded(key):
    assert key in config.SYNC_EXCLUDE, f"{key} would sync between devices"


@pytest.mark.parametrize("key", ACCESS_KEYS)
def test_access_credentials_are_stripped_from_an_outgoing_pull(key, monkeypatch):
    """A server's pull payload must not hand its own login to the desktop."""
    config.save_settings({key: "sentinel-value"})
    data, _mtime = config.get_settings_for_sync()
    assert key not in data


@pytest.mark.parametrize("key", ACCESS_KEYS)
def test_access_credentials_in_an_incoming_push_are_ignored(key):
    """The exact failure: a push must not overwrite who can log in here."""
    config.save_settings({key: "mine"})
    config.merge_synced_settings({key: "theirs", "poll_interval_minutes": 42})
    assert config.get_settings()[key] == "mine"
    # ...while an ordinary setting still merges, so the guard is not too broad.
    assert config.get_settings()["poll_interval_minutes"] == 42


def test_api_key_revocation_cannot_ride_a_push():
    """The live symptom: the desktop's key list wiped the server's."""
    config.save_settings({"auth_api_keys": [{"name": "server", "prefix": "pp_keepme"}]})
    config.merge_synced_settings({"auth_api_keys": []})
    keys = config.get_settings()["auth_api_keys"]
    assert keys and keys[0]["prefix"] == "pp_keepme"


def test_platform_credentials_still_sync():
    """Guard against over-correcting — pairing exists to move these."""
    for key in ("fa_cookie_a", "sf_api_token", "ws_api_key", "password"):
        assert key not in config.SYNC_EXCLUDE
    config.merge_synced_settings({"fa_cookie_a": "from-server"})
    assert config.get_settings()["fa_cookie_a"] == "from-server"


# ── Host-specific filesystem locations (3.5.4) ─────────────────
# These describe the BOX, not the install. A Windows archive path is meaningless
# on the Linux VM and vice versa, yet all four crossed the sync — contained only
# by a downstream os.path.isdir() happening to fail on the foreign value.

HOST_PATH_KEYS = [
    "posting_story_archive_path",
    "artwork_archive_path",
    "auto_backup_dir",
    "ig_public_base_url",
]


@pytest.mark.parametrize("key", HOST_PATH_KEYS)
def test_host_paths_are_excluded(key):
    assert key in config.SYNC_EXCLUDE, f"{key} would cross machines"


@pytest.mark.parametrize("key", HOST_PATH_KEYS)
def test_host_paths_are_stripped_from_an_outgoing_pull(key):
    config.save_settings({key: r"C:\Users\someone\Archives"})
    data, _mtime = config.get_settings_for_sync()
    assert key not in data


@pytest.mark.parametrize("key", HOST_PATH_KEYS)
def test_an_incoming_push_cannot_relocate_this_instances_data(key):
    """A desktop push must not repoint the server's archive at a Windows path."""
    config.save_settings({key: "/app/data/artwork"})
    config.merge_synced_settings({key: r"C:\Users\someone\artwork"})
    assert config.get_settings()[key] == "/app/data/artwork"
