"""Updated FA cookies must take effect without a restart (3.19.2).

Live failure, in sequence: fresh cookies were pasted into Settings, two posts
were attempted, both failed immediately with the stale session. Two separate
traps produced that:

  1. **The paste landed on the wrong account.** The main FurAffinity form
     writes the BARE credential keys, which belong to the *default* account
     (2, KnaughtyKat). The post ran as account 15 (SecondFur), which resolves
     its own `acct_15_*` keys — still holding the dead cookies. Verified live
     by fingerprinting both credential sets: bare == account 2 (fresh, valid),
     account 15 stale and invalid.

  2. **Even a correct paste would not have taken effect.** `_ensure_client`
     was `if self._client: return self._client`, the manager caches poster
     instances per (platform, account_id) — and worse, the old code assigned
     `self._client` BEFORE validating, so a failed validation left a POISONED
     cache that skipped validation on every later call. The only cure was a
     container restart nobody knew was needed.

The cache is now keyed on a fingerprint of the resolved credentials, rebuilt
only when they change, and assigned only after validation succeeds.
"""
from __future__ import annotations

import pytest

import config
from posting.platforms.furaffinity import FurAffinityPoster


class _FakeAccounts:
    @staticmethod
    def get_account(conn, aid):
        return {"account_id": aid, "is_default": aid == 2}

    @staticmethod
    def get_default_account_id(conn, platform, create=True):
        return 2


class _FakeConn:
    def close(self): pass


@pytest.fixture()
def poster(monkeypatch):
    import database.accounts as accdb
    import database.db as db
    monkeypatch.setattr(accdb, "get_account", _FakeAccounts.get_account)
    monkeypatch.setattr(accdb, "get_default_account_id",
                        _FakeAccounts.get_default_account_id)
    monkeypatch.setattr(db, "get_connection", lambda: _FakeConn())
    return FurAffinityPoster(account_id=15)


def _with_creds(monkeypatch, a, b, valid=True):
    monkeypatch.setattr(config, "resolve_account_credentials",
                        lambda plat, aid, dflt, settings=None: {
                            "fa_username": "SecondFur",
                            "fa_cookie_a": a, "fa_cookie_b": b})
    import clients.fa.client as fac

    async def _validate(self):
        return valid
    monkeypatch.setattr(fac.FAClient, "validate_cookies", _validate)


def test_new_cookies_take_effect_without_a_restart(monkeypatch, poster):
    """The headline. Build with old cookies, change the settings, next call
    must use the new ones."""
    import asyncio
    _with_creds(monkeypatch, "old_a", "old_b")
    c1 = asyncio.run(poster._ensure_client())
    assert c1.cookie_a == "old_a"

    _with_creds(monkeypatch, "new_a", "new_b")
    c2 = asyncio.run(poster._ensure_client())
    assert c2.cookie_a == "new_a", "a cookie paste must not require a restart"
    assert c2 is not c1


def test_unchanged_cookies_reuse_the_client(monkeypatch, poster):
    import asyncio
    _with_creds(monkeypatch, "a1", "b1")
    c1 = asyncio.run(poster._ensure_client())
    c2 = asyncio.run(poster._ensure_client())
    assert c2 is c1, "same credentials must not rebuild the client"


def test_a_failed_validation_does_not_poison_the_cache(monkeypatch, poster):
    """⚠ The old code set `self._client` BEFORE validating, so one failed
    validation left a cached client that every later call returned without
    re-checking. That is why re-pasting cookies could never fix a poster that
    had once seen bad ones."""
    import asyncio
    _with_creds(monkeypatch, "dead_a", "dead_b", valid=False)
    with pytest.raises(RuntimeError):
        asyncio.run(poster._ensure_client())
    assert poster._client is None, "a failed validation must cache nothing"

    _with_creds(monkeypatch, "fresh_a", "fresh_b", valid=True)
    c = asyncio.run(poster._ensure_client())
    assert c.cookie_a == "fresh_a", "recovery must need no restart"


def test_the_error_names_the_wrong_account_trap(monkeypatch, poster):
    """The paste-to-the-wrong-account mistake is invisible unless the error
    explains it: a non-default account reads its OWN fields, and the main
    FurAffinity form updates the DEFAULT account."""
    import asyncio
    _with_creds(monkeypatch, "dead_a", "dead_b", valid=False)
    with pytest.raises(RuntimeError) as e:
        asyncio.run(poster._ensure_client())
    msg = str(e.value)
    assert "account 15" in msg
    assert "DEFAULT" in msg or "default" in msg


def test_missing_cookies_on_a_non_default_account_say_where_to_paste(monkeypatch, poster):
    import asyncio
    _with_creds(monkeypatch, "", "")
    with pytest.raises(RuntimeError) as e:
        asyncio.run(poster._ensure_client())
    assert "account 15" in str(e.value)
    assert "OWN credential fields" in str(e.value)
