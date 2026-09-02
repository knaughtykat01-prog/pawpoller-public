"""DeviantArt counts as configured on OAuth alone (3.9.4).

2.47.0 moved DA polling to the official OAuth API and demoted the browser
cookie to a legacy `_napi` fallback. `da_poller` was updated; three
"is DA configured?" predicates elsewhere were not, and each still demanded
`da_cookie`. On an OAuth-only install that meant:

* no DA account was ever seeded — and with no account there is nothing to hang
  a per-account posting token on;
* every *scheduled* DA poll was skipped, while a hand-triggered poll worked,
  which reads like a scheduler bug;
* the status table reported DeviantArt as unconfigured.

The tests are written against the OAuth-only shape because that is the one
that was broken, and the legacy shape is asserted alongside so demoting the
cookie further does not quietly drop it.
"""
from __future__ import annotations

import pytest

from database.accounts import DEFAULT_CRED_CHECKS

_OAUTH = {"da_client_id": "75305", "da_client_secret": "s", "da_target_user": "secondfur"}
_COOKIE = {"da_cookie": "auth=x; auth_secure=y", "da_target_user": "secondfur"}


def _da_status_check():
    """The predicate the poll-status table uses, pulled out of its tuple."""
    from routes.api import _PLATFORM_HEALTH_CONFIG
    for spec in _PLATFORM_HEALTH_CONFIG:
        if spec[0] == "da":
            return spec[4]
    raise AssertionError("no DA entry in the poll-status table")


@pytest.mark.parametrize("check_name", ["seed", "status"])
def test_oauth_alone_counts_as_configured(check_name):
    check = DEFAULT_CRED_CHECKS["da"] if check_name == "seed" else _da_status_check()
    assert check(_OAUTH) is True, (
        "OAuth is the real DA path since 2.47.0 — requiring the legacy cookie "
        "here is what stopped an account being seeded and skipped scheduled polls."
    )


@pytest.mark.parametrize("check_name", ["seed", "status"])
def test_the_legacy_cookie_still_counts(check_name):
    check = DEFAULT_CRED_CHECKS["da"] if check_name == "seed" else _da_status_check()
    assert check(_COOKIE) is True


@pytest.mark.parametrize("check_name", ["seed", "status"])
def test_neither_credential_is_not_configured(check_name):
    check = DEFAULT_CRED_CHECKS["da"] if check_name == "seed" else _da_status_check()
    assert not check({"da_target_user": "secondfur"})


@pytest.mark.parametrize("check_name", ["seed", "status"])
def test_a_half_configured_app_is_not_enough(check_name):
    """client_id without client_secret cannot mint a token, so it is not a
    credential — it is a typo, and reporting it as configured hides that."""
    check = DEFAULT_CRED_CHECKS["da"] if check_name == "seed" else _da_status_check()
    assert not check({"da_client_id": "75305", "da_target_user": "secondfur"})


@pytest.mark.parametrize("check_name", ["seed", "status"])
def test_the_target_user_is_still_required(check_name):
    """Credentials say who we are; target_user says whose gallery to read."""
    check = DEFAULT_CRED_CHECKS["da"] if check_name == "seed" else _da_status_check()
    assert not check({"da_client_id": "75305", "da_client_secret": "s"})


def test_the_three_gates_agree_with_each_other():
    """The bug was three copies of one rule drifting apart. Pin them together."""
    seed = DEFAULT_CRED_CHECKS["da"]
    status = _da_status_check()
    for settings in (_OAUTH, _COOKIE, {}, {"da_target_user": "x"},
                     {"da_client_id": "1", "da_target_user": "x"}):
        assert bool(seed(settings)) == bool(status(settings)), settings
