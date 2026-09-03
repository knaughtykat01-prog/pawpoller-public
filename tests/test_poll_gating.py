"""A platform nobody connected was polled anyway (4.3.6).

From a tester's log, on an install that has never had an AO3 credential in it:

    AO3: HTTP 403 — "Shields are up!"

She does not use AO3. The poller ran because ``poll_platform_accounts`` ended
with a fallback written before account rows were seeded *from* credentials:

    if not accts:
        await run_cycle()      # poll the default account

Once ``seed_default_accounts`` began skipping platforms with no credentials,
"no account rows" stopped meaning "an old install that predates accounts" and
started meaning "not configured" — so the fallback fired for every platform the
user had never touched, sending a real request to a site they never connected.
AO3 throttles per IP, so the app was also spending goodwill on their behalf.

The docstring above ``poll_platform_accounts`` said the cycles "self-skip if
uncredentialed". The AO3 cycle does not; it authenticates with an empty cookie
and lets the site answer. A comment asserting a guard is not a guard.
"""
from __future__ import annotations

import asyncio

import pytest

import config
from database import accounts as accounts_db
from polling import multi_account


@pytest.fixture()
def poll(monkeypatch):
    """Drive poll_platform_accounts over a table we control, and record every
    cycle it decides to run."""
    state = {"settings": {}, "rows": [], "raise_on_enumerate": False}
    ran: list = []

    class _Conn:
        def close(self):
            pass

    def _get_connection():
        if state["raise_on_enumerate"]:
            raise RuntimeError("database is locked")
        return _Conn()

    monkeypatch.setattr("database.db.get_connection", _get_connection)
    monkeypatch.setattr(accounts_db, "seed_default_accounts", lambda conn, s: 0)
    monkeypatch.setattr(accounts_db, "list_accounts",
                        lambda conn, platform=None, enabled_only=False: list(state["rows"]))
    monkeypatch.setattr(config, "get_settings", lambda: dict(state["settings"]))
    monkeypatch.setattr(config, "resolve_account_credentials",
                        lambda p, aid, is_def, s: dict(s))

    async def _run_cycle(account_id=None, force_full=False):
        ran.append(account_id)
        return {}

    def _drive(platform="ao3", **kw):
        state.update(kw)
        ran.clear()
        asyncio.run(multi_account.poll_platform_accounts(platform, run_cycle=_run_cycle))
        return ran

    _drive.state = state
    return _drive


# Credentials that satisfy DEFAULT_CRED_CHECKS["ao3"].
AO3_CREDS = {"ao3_username": "Penwright", "ao3_password": "x"}


def _row(account_id=1, platform="ao3", enabled=1, is_default=1):
    return {"account_id": account_id, "platform": platform, "enabled": enabled,
            "is_default": is_default, "label": ""}


class TestTheReportedCase:
    def test_an_unconfigured_platform_is_not_polled(self, poll):
        assert poll(settings={}, rows=[]) == [], (
            "no credentials and no accounts means the user never connected this "
            "site — polling it sends a real request on their behalf")

    def test_the_credential_check_is_the_one_the_rest_of_the_app_uses(self):
        """Not a second opinion about what 'configured' means."""
        src = open("polling/multi_account.py", encoding="utf-8").read()
        assert "accounts_db.DEFAULT_CRED_CHECKS.get(platform" in src

    def test_the_docstring_no_longer_claims_a_guard_that_is_not_there(self):
        """It said "the cycle self-skips if uncredentialed". The AO3 cycle does
        not, which is how the fallback survived being read several times."""
        src = open("polling/multi_account.py", encoding="utf-8").read()
        assert "self-skips if uncredentialed" not in src.split("⚠")[0]


class TestWhatMustStillPoll:
    def test_a_configured_install_with_no_account_rows_still_polls(self, poll):
        """The original point of the fallback: an install whose account rows
        have not been seeded yet must not go quiet."""
        assert poll(settings=AO3_CREDS, rows=[]) == [None]

    def test_each_enabled_account_is_polled(self, poll):
        rows = [_row(1), _row(2, is_default=0)]
        assert poll(settings=AO3_CREDS, rows=rows) == [1, 2]

    def test_an_account_without_credentials_is_skipped_as_before(self, poll, monkeypatch):
        monkeypatch.setattr(config, "resolve_account_credentials",
                            lambda p, aid, is_def, s: AO3_CREDS if aid == 1 else {})
        assert poll(settings=AO3_CREDS, rows=[_row(1), _row(2, is_default=0)]) == [1]


class TestDisabledAccounts:
    def test_switching_every_account_off_stops_the_poll(self, poll):
        """Disabling an account on the Accounts page is a decision; the old
        fallback polled the default anyway, because zero *enabled* rows looked
        the same as zero rows."""
        assert poll(settings=AO3_CREDS, rows=[_row(1, enabled=0)]) == []

    def test_one_left_on_still_polls(self, poll):
        rows = [_row(1, enabled=0), _row(2, enabled=1, is_default=0)]
        assert poll(settings=AO3_CREDS, rows=rows) == [2]


class TestEnumerationFailure:
    def test_a_broken_table_still_polls_a_configured_platform(self, poll):
        """A database problem must not silently stop a real user's polling."""
        assert poll(settings=AO3_CREDS, rows=[], raise_on_enumerate=True) == [None]

    def test_but_not_an_unconfigured_one(self, poll):
        assert poll(settings={}, rows=[], raise_on_enumerate=True) == []


class TestEveryPlatformHasACheck:
    def test_no_platform_falls_through_to_lambda_true(self):
        """``DEFAULT_CRED_CHECKS.get(platform, lambda s: True)`` is a real
        default: a platform missing from the table would be polled
        unconditionally, which is the bug this file is about."""
        missing = [c for c in multi_account.get_poll_cycles()
                   if c not in accounts_db.DEFAULT_CRED_CHECKS]
        assert missing == [], f"no credential check for: {missing}"

    def test_a_single_account_manual_poll_is_untouched(self, poll):
        """Clicking Poll Now on one account is an explicit instruction and
        still runs, credentials or not — the user is looking at the result."""
        ran: list = []

        async def _cycle(account_id=None, force_full=False):
            ran.append(account_id)

        asyncio.run(multi_account.poll_platform_accounts("ao3", 7, run_cycle=_cycle))
        assert ran == [7]
