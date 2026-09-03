"""A browser-login form field must be a field something reads (4.3.3).

A tester's X account would not stay connected: logging in through the browser
window succeeded, the log said `Saved browser login credentials for tw:
['tw_auth_token', 'tw_ct0', 'tw_username']`, and the very next poll failed with
*"credential validation failed — update the auth_token/ct0 cookies"*. The
cookies were fine. The USERNAME was saved under `tw_username`, and every
consumer — the poller, the auth-status endpoint, the poster — reads
`tw_target_user`. Nothing in the codebase has ever read `tw_username`.

With no username, both cookie backends look up an empty screen name and fail,
which is indistinguishable from a dead cookie. So the app told the user to
replace credentials that were working, every time, forever.

DeviantArt had the identical defect (`da_username` vs `da_target_user`).
FurAffinity did not, because `fa_username` happens to be canonical — which is
why this went unnoticed: the pattern looked right.

The same shape as the five hand-written lists of §59.7: one fact, declared in
two places, with nothing checking they agree.
"""
from __future__ import annotations

import re

import pytest

import config
from auth.browser_login import PLATFORM_LOGIN


def _fields(spec) -> list[str]:
    """The form's field ids. The key is ``fields`` — an earlier draft of this
    test guessed ``extra_fields`` and every case passed VACUOUSLY on an empty
    list, which is the same species of mistake as the bug it guards."""
    assert "fields" in spec or "url" in spec, "PLATFORM_LOGIN shape changed"
    return [f["id"] for f in (spec.get("fields") or [])]


class TestFormFieldsAreCredentialFields:
    @pytest.mark.parametrize("platform", sorted(PLATFORM_LOGIN))
    def test_every_declared_field_is_one_the_app_reads(self, platform):
        """THE bug. The field id doubles as the settings key it is saved under,
        so a non-canonical id writes to a key with no readers."""
        canonical = config.PLATFORM_CREDENTIAL_FIELDS.get(platform, [])
        assert canonical, f"{platform} has no canonical credential fields"
        for field in _fields(PLATFORM_LOGIN[platform]):
            assert field in canonical, (
                f"browser login saves {platform!r} field {field!r}, but the credential "
                f"fields are {canonical} — nothing will ever read it")

    def test_the_two_that_were_wrong_are_named_explicitly(self):
        assert "tw_target_user" in _fields(PLATFORM_LOGIN["tw"])
        assert "da_target_user" in _fields(PLATFORM_LOGIN["da"])
        for platform in ("tw", "da"):
            assert f"{platform}_username" not in _fields(PLATFORM_LOGIN[platform])

    def test_a_required_field_is_required_because_polling_needs_it(self):
        """target_user is not decoration: both cookie backends look the account
        up by screen name."""
        for platform in ("tw", "da"):
            spec = next(f for f in PLATFORM_LOGIN[platform]["fields"]
                        if f["id"] == f"{platform}_target_user")
            assert spec.get("required") is True


class TestFrontendSendsTheSameKey:
    """The browser-login call passes extra_fields straight through to settings,
    so the frontend's key must match the form's id."""

    def test_the_ui_sends_canonical_keys(self):
        js = open("frontend/js/app.js", encoding="utf-8").read()
        for platform in ("tw", "da"):
            calls = re.findall(rf"browserLogin\('{platform}',\s*\{{\s*(\w+):", js)
            assert calls, f"no browserLogin call for {platform}"
            for key in calls:
                assert key in config.PLATFORM_CREDENTIAL_FIELDS[platform], (
                    f"app.js sends {key!r} for {platform}; nothing reads it")


class TestMigration:
    @pytest.fixture()
    def settings(self, monkeypatch, tmp_path):
        store = {}
        monkeypatch.setattr(config, "get_settings", lambda: dict(store))
        monkeypatch.setattr(config, "save_settings", lambda d: store.update(d))
        return store

    def test_a_stranded_username_is_moved_not_retyped(self, settings):
        settings.update({"tw_username": "SecondFur", "tw_auth_token": "a", "tw_ct0": "b"})
        assert config.migrate_browser_login_usernames() == 1
        assert settings["tw_target_user"] == "SecondFur"

    def test_deviantart_too(self, settings):
        settings.update({"da_username": "Inkwolf"})
        config.migrate_browser_login_usernames()
        assert settings["da_target_user"] == "Inkwolf"

    def test_per_account_copies_are_moved(self, settings):
        """Non-default accounts namespace the key; the stale one does too."""
        settings.update({"acct_3_tw_username": "ThirdFur"})
        config.migrate_browser_login_usernames()
        assert settings["acct_3_tw_target_user"] == "ThirdFur"

    def test_an_existing_value_is_never_overwritten(self, settings):
        """Someone who typed the username into Settings by hand has the right
        value already — the stale key may hold something older."""
        settings.update({"tw_username": "old", "tw_target_user": "current"})
        assert config.migrate_browser_login_usernames() == 0
        assert settings["tw_target_user"] == "current"

    def test_empty_values_are_not_migrated(self, settings):
        settings.update({"tw_username": "", "da_username": ""})
        assert config.migrate_browser_login_usernames() == 0
        assert "tw_target_user" not in settings

    def test_it_is_safe_to_run_twice(self, settings):
        settings.update({"tw_username": "SecondFur"})
        assert config.migrate_browser_login_usernames() == 1
        assert config.migrate_browser_login_usernames() == 0

    def test_nothing_to_do_is_a_no_op(self, settings):
        settings.update({"tw_target_user": "SecondFur"})
        assert config.migrate_browser_login_usernames() == 0

    def test_it_runs_on_startup_in_both_entry_points(self):
        for f in ("server.py", "dashboard.py"):
            assert "migrate_browser_login_usernames()" in open(f, encoding="utf-8").read(), (
                f"{f} does not run the migration, so that install never gets the fix")


class TestTheErrorTellsTheTruth:
    def test_a_missing_username_is_not_reported_as_a_cookie_problem(self):
        """§55: a check that confirms the wrong thing is worse than one that
        fails. This one sent a user to re-copy working cookies, repeatedly."""
        src = open("polling/tw_poller.py", encoding="utf-8").read()
        i = src.index("valid = await client.validate_cookies()")
        before = src[:i]
        assert "not client.target_user" in before, (
            "the username must be checked BEFORE validation blames the cookies")
        assert "X/Twitter username is not set" in before
