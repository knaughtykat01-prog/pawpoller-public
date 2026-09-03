"""The registries that decide whether a platform is polled at all.

A poller can be complete, tested and correct and still never run once. The
orchestrator schedules a platform by enumerating its ENABLED ACCOUNTS, so a
platform missing from ``accounts.PLATFORMS`` is never given any accounts to
enumerate and simply never appears in a cycle — silently, with no error, no log
line and no status dot.

That is not hypothetical. Furbooru shipped in 2.201.0 with a client, a poller,
a schema, a metrics entry and a poll-cycle registration, and was still absent
from ``PLATFORMS`` a version later. Production held zero Furbooru accounts and
an empty ``fbr_poll_log``: it had never polled a single time. FurryNetwork was
in the same state and escaped only because a manual "Poll Now" happens to fall
back to a default-account poll, which created its account as a side effect.

Each test below is one of the lists that has to agree.
"""
from __future__ import annotations

from database import accounts as adb
from database import platform_metrics as pm
from polling.multi_account import get_poll_cycles


def test_every_scheduled_platform_can_hold_accounts():
    """THE bug. A cycle registered here but absent from PLATFORMS is dead
    code — the orchestrator has no accounts to hand it."""
    missing = sorted(set(get_poll_cycles()) - set(adb.PLATFORMS))
    assert not missing, (
        f"{missing} have poll cycles but cannot hold accounts, so the "
        f"orchestrator will never schedule them")


def test_every_account_platform_has_a_credential_predicate():
    """Without one, ``_poll_accounts`` falls back to ``lambda s: True`` and
    polls an account with no credentials every cycle."""
    missing = [p for p in adb.PLATFORMS if p not in adb.DEFAULT_CRED_CHECKS]
    assert not missing, f"no DEFAULT_CRED_CHECKS entry for {missing}"


def test_every_account_platform_has_a_display_name():
    missing = [p for p in adb.PLATFORMS if p not in adb.PLATFORM_NAMES]
    assert not missing, f"no PLATFORM_NAMES entry for {missing}"


def test_every_account_platform_has_a_handle_key():
    """(platform, handle) is the natural key for desktop<->server mirroring.
    With no handle key an account can only be matched on is_default, so a
    second account added on one machine inserts as a NEW row on the other."""
    missing = [p for p in adb.PLATFORMS if p not in adb._HANDLE_KEYS]
    assert not missing, f"no _HANDLE_KEYS entry for {missing}"


def test_the_pause_toggle_accepts_every_scheduled_platform():
    """Pausing returned 400 "Unknown platform" for fn, fbr and tg while all
    three were being polled — the toggle's list was hand-written and stopped
    at e621."""
    from routes import api
    missing = sorted(set(get_poll_cycles()) - set(api._PAUSEABLE_PLATFORMS))
    assert not missing, f"cannot pause {missing}"


def test_the_health_endpoint_reports_every_scheduled_platform():
    """No entry here means no status dot and no "last polled · next in" —
    the platform polls invisibly."""
    from routes import api
    covered = {code for code, *_ in api._PLATFORM_HEALTH_CONFIG}
    missing = sorted(set(get_poll_cycles()) - covered)
    assert not missing, f"no health entry for {missing}"


def test_the_health_config_names_functions_that_exist():
    """The entries carry a module and a function NAME resolved at request
    time, so a typo is invisible until the endpoint is called."""
    from routes import api
    for code, module, fn_name, _interval, _configured in api._PLATFORM_HEALTH_CONFIG:
        assert hasattr(module, fn_name), f"{code}: {module.__name__} has no {fn_name}"


def test_settings_keys_cover_every_platform_the_ui_can_render():
    """Both allow-lists in routes/api.py are derived from the registry now.
    Before that they were hand-listed and stopped at e621, so an interval or
    notification toggle for fn/fbr/tg round-tripped in the form and was
    dropped on save with no error."""
    for suffix in ("poll_interval_minutes", "notifications_enabled"):
        keys = pm.setting_keys(suffix)
        assert len(keys) == len(pm.ALL_CODES)
        # Inkbunny predates the prefix convention.
        assert keys[0] == suffix
        assert f"tg_{suffix}" in keys
        assert f"fbr_{suffix}" in keys
