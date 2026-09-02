"""Re-authorising a DeviantArt account has to take effect now (3.32.1).

Reported the morning after 3.32.0 shipped: *"ive reauthorised both accounts but
i keep getting error saying its the wrong account"*.

The re-authorisation had worked. The log shows it landing on the right key —
`stored a refresh token from the authorisation-code flow (key=acct_27_…)` — and
the very next post still refused, naming the account the **old** token belonged
to.

Posters live in a module-level cache keyed by `(platform, account_id)` and the
access token is good for an hour. `_ensure_client` refreshed only when that
token had expired, so a freshly stored `da_refresh_token` was simply never
read: the stale access token stayed valid, `_token_owner` stayed cached, and
3.32.0's identity guard kept reporting a verdict about a credential the user
had already replaced. The guard was right about the token it held and wrong
about the world.

⚠ The fix cannot fingerprint the refresh token it *sends*. DA rotates on every
refresh, so the stored value differs from the sent one immediately afterwards —
comparing against the sent token would force a refresh on every single call and
burn a single-use credential per post. The fingerprint is therefore taken
**after** the refresh, from the value that was just stored.
"""
from __future__ import annotations

import asyncio
import time

import pytest


class _FakeClient:
    def __init__(self, target_user, owner_by_token):
        self.target_user = target_user
        self._owner_by_token = owner_by_token
        self.refreshes = 0

    async def oauth_refresh_token(self, cid, secret, refresh):
        self.refreshes += 1
        # DA rotates: every refresh returns a NEW refresh token.
        return {"access_token": "access-for-" + refresh,
                "refresh_token": refresh + "+rotated",
                "expires_in": 3600}

    async def whoami(self, token):
        return {"username": self._owner_by_token(token)}


def _poster(creds, owner_by_token):
    from posting.platforms.deviantart import DeviantArtPoster
    p = DeviantArtPoster()
    saved = []

    p._resolve_creds = lambda *a, **k: dict(creds)
    p._client = _FakeClient(creds.get("da_target_user", ""), owner_by_token)

    def _save(platform, values):
        saved.append(values)
        creds.update(values)          # mirrors settings being written back

    p._save_creds = _save
    p.saved = saved
    return p


def _owner_from_token(token):
    """The old refresh token authorises the wrong account; the new one is
    correct — the exact shape of the live incident."""
    return "SecondFur" if "reauthorised" in token else "ThirdFur"


# ── the regression ───────────────────────────────────────────────────

def test_a_new_refresh_token_is_picked_up_without_waiting_an_hour():
    """THE regression. The access token is still valid, so nothing forced a
    refresh, and the guard kept judging a credential that no longer existed."""
    creds = {"da_client_id": "id", "da_client_secret": "s",
             "da_refresh_token": "old", "da_target_user": "SecondFur"}
    p = _poster(creds, _owner_from_token)

    with pytest.raises(RuntimeError, match="posts as ThirdFur"):
        asyncio.run(p._ensure_client())

    # The user re-authorises: a new refresh token lands on this account's key.
    creds["da_refresh_token"] = "reauthorised"

    client, token = asyncio.run(p._ensure_client())
    assert token == "access-for-reauthorised"
    assert p._token_owner == "SecondFur"


def test_an_unchanged_credential_does_not_force_a_refresh():
    """DA refresh tokens are single-use. Re-refreshing on every call would burn
    one credential per post — worse than the bug being fixed."""
    creds = {"da_client_id": "id", "da_client_secret": "s",
             "da_refresh_token": "reauthorised", "da_target_user": "SecondFur"}
    p = _poster(creds, _owner_from_token)

    asyncio.run(p._ensure_client())
    asyncio.run(p._ensure_client())
    asyncio.run(p._ensure_client())
    assert p._client.refreshes == 1


def test_the_fingerprint_follows_the_rotated_token_not_the_sent_one():
    """The subtle half. DA returns a new refresh token on every refresh, so a
    fingerprint of what we SENT would differ immediately and re-refresh
    forever."""
    creds = {"da_client_id": "id", "da_client_secret": "s",
             "da_refresh_token": "reauthorised", "da_target_user": "SecondFur"}
    p = _poster(creds, _owner_from_token)
    asyncio.run(p._ensure_client())

    assert creds["da_refresh_token"] == "reauthorised+rotated", \
        "the rotation should have been written back"
    assert p._cred_fp == p._fingerprint("id", "s", "reauthorised+rotated")

    asyncio.run(p._ensure_client())
    assert p._client.refreshes == 1, "the rotation must not look like a change"


def test_an_expired_token_still_refreshes_normally():
    creds = {"da_client_id": "id", "da_client_secret": "s",
             "da_refresh_token": "reauthorised", "da_target_user": "SecondFur"}
    p = _poster(creds, _owner_from_token)
    asyncio.run(p._ensure_client())
    p._token_expires_at = time.time() - 1
    asyncio.run(p._ensure_client())
    assert p._client.refreshes == 2


def test_the_owner_is_rechecked_after_a_credential_change():
    """A cached `_token_owner` outliving its token is how the wrong verdict
    survived the fix."""
    creds = {"da_client_id": "id", "da_client_secret": "s",
             "da_refresh_token": "reauthorised", "da_target_user": "SecondFur"}
    p = _poster(creds, _owner_from_token)
    asyncio.run(p._ensure_client())
    assert p._token_owner == "SecondFur"

    creds["da_refresh_token"] = "old"          # swapped back to the wrong one
    with pytest.raises(RuntimeError, match="posts as ThirdFur"):
        asyncio.run(p._ensure_client())


def test_a_changed_target_user_reaches_the_cached_client():
    """Posters are cached for the life of the process, so a renamed target
    would otherwise stay stale forever — and drive a false mismatch."""
    creds = {"da_client_id": "id", "da_client_secret": "s",
             "da_refresh_token": "reauthorised", "da_target_user": "SecondFur"}
    p = _poster(creds, _owner_from_token)
    asyncio.run(p._ensure_client())

    creds["da_target_user"] = "secondfur"
    asyncio.run(p._ensure_client())
    assert p._client.target_user == "secondfur"


def test_the_fingerprint_does_not_hold_the_secret():
    """Hashed rather than stored, so a refresh token never sits in a second
    place in memory and cannot reach a log line or a repr."""
    from posting.platforms.deviantart import DeviantArtPoster
    fp = DeviantArtPoster._fingerprint("id", "secret", "refresh-token")
    assert "refresh-token" not in fp and "secret" not in fp
    assert len(fp) == 64
    assert fp != DeviantArtPoster._fingerprint("id", "secret", "other")


def test_the_parts_cannot_be_run_together_into_the_same_digest():
    """A plain concatenation would make ("ab", "c") and ("a", "bc") identical —
    a rotation could then read as no change."""
    from posting.platforms.deviantart import DeviantArtPoster
    assert (DeviantArtPoster._fingerprint("ab", "c", "d")
            != DeviantArtPoster._fingerprint("a", "bc", "d"))
