"""Manual per-account poll dispatch (polling/multi_account.py).

Backs the account picker on the dashboard poll button: a manual poll can target
one account or every enabled account for a platform, instead of only the
platform default. Tests use an injected fake cycle so nothing hits the network.
"""

import config
import pytest

from polling.multi_account import get_poll_cycles, poll_platform_accounts


def test_registry_has_all_platforms():
    assert set(get_poll_cycles().keys()) == {
        "ib", "fa", "ws", "da", "wp", "ik", "bsky", "tw", "sf", "sqw",
        "ao3", "mast", "tum", "pix", "thr", "ig", "e621", "fn", "fbr",
    }


@pytest.mark.asyncio
async def test_single_account_polls_only_that_account():
    called = []

    async def fake_cycle(account_id=None):
        called.append(account_id)

    await poll_platform_accounts("tw", 13, run_cycle=fake_cycle)
    assert called == [13]


@pytest.mark.asyncio
async def test_unknown_platform_raises():
    with pytest.raises(ValueError):
        await poll_platform_accounts("nope", None)


@pytest.mark.asyncio
async def test_no_accounts_falls_back_to_default(monkeypatch):
    # Fresh DB has no enabled accounts for a bare platform → single default poll.
    called = []

    async def fake_cycle(account_id=None):
        called.append(account_id)

    await poll_platform_accounts("tw", None, run_cycle=fake_cycle)
    assert called == [None]  # one default-account poll, no account_id


@pytest.mark.asyncio
async def test_all_accounts_polls_each_enabled_account(monkeypatch):
    from database.db import get_connection
    from database import accounts as accounts_db

    # Neutralise credential resolution + the per-platform cred gate so both
    # accounts are considered pollable regardless of vault state.
    monkeypatch.setattr(config, "resolve_account_credentials", lambda *a, **k: {})
    monkeypatch.setitem(accounts_db.DEFAULT_CRED_CHECKS, "tw", lambda creds: True)

    conn = get_connection()
    try:
        a1 = accounts_db.create_account(conn, "tw", "One", is_default=True)
        a2 = accounts_db.create_account(conn, "tw", "Two")
    finally:
        conn.close()

    called = []

    async def fake_cycle(account_id=None):
        called.append(account_id)

    await poll_platform_accounts("tw", None, run_cycle=fake_cycle)
    assert a1 in called and a2 in called
