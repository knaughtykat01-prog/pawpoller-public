"""Inkbunny posting reuses the account's cached session, user_id included.

The poster read only the cached SID, never its user_id, so ensure_session judged a good session
"user_id unknown" and logged in again before every post. The fresh SID was then refused by the
upload a second later ("Invalid Session ID") and the post went through only on the one-minute
retry. Nothing here reaches the network: InkbunnyClient's two calls are stood in for.
"""
from __future__ import annotations

import asyncio

import pytest

import config
from database import queries
from database.db import get_connection
from posting.platforms import inkbunny as ibp


@pytest.fixture
def ib_account(monkeypatch):
    from database import accounts
    conn = get_connection()
    try:
        aid = accounts.get_default_account_id(conn, "ib", create=True)
    finally:
        conn.close()
    monkeypatch.setattr(config, "resolve_account_credentials",
                        lambda *a, **k: {"username": "SecondFur", "password": "pw"})
    return aid


class _Probe:
    """Counts logins; the cached-session probe answers 'still good'."""

    def __init__(self, monkeypatch, alive=True):
        self.logins = 0
        probe = self

        async def _post(self_http, url, data=None, **kw):
            class R:
                def json(self):
                    return {} if alive else {"error_code": 2}
            return R()

        async def _login(client):
            probe.logins += 1
            client.sid, client.user_id = "NEWSID", 4242
            return type("L", (), {"sid": "NEWSID"})()
        monkeypatch.setattr(ibp.InkbunnyClient, "login", _login)
        monkeypatch.setattr("httpx.AsyncClient.post", _post)


def _cache(aid, sid, user_id):
    conn = get_connection()
    try:
        queries.save_session(conn, aid, sid, "SecondFur", user_id)
    finally:
        conn.close()


def _cached(aid):
    conn = get_connection()
    try:
        return queries.get_cached_session(conn, aid)
    finally:
        conn.close()


def test_a_good_cached_session_with_its_user_id_is_reused_without_a_login(monkeypatch, ib_account):
    _cache(ib_account, "OLDSID", 4242)
    probe = _Probe(monkeypatch, alive=True)
    client = asyncio.run(ibp.InkbunnyPoster(ib_account)._ensure_client())
    assert probe.logins == 0 and client.sid == "OLDSID" and client.user_id == 4242


def test_a_cached_session_missing_its_user_id_logs_in_once_and_saves_it(monkeypatch, ib_account):
    _cache(ib_account, "OLDSID", 0)
    probe = _Probe(monkeypatch, alive=True)
    asyncio.run(ibp.InkbunnyPoster(ib_account)._ensure_client())
    assert probe.logins == 1
    assert _cached(ib_account)["sid"] == "NEWSID" and _cached(ib_account)["user_id"] == 4242
    asyncio.run(ibp.InkbunnyPoster(ib_account)._ensure_client())         # the next post reuses it
    assert probe.logins == 1


def test_an_expired_session_logs_in_and_is_saved(monkeypatch, ib_account):
    _cache(ib_account, "OLDSID", 4242)
    probe = _Probe(monkeypatch, alive=False)
    asyncio.run(ibp.InkbunnyPoster(ib_account)._ensure_client())
    assert probe.logins == 1 and _cached(ib_account)["sid"] == "NEWSID"
