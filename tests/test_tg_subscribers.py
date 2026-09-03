"""Telegram subscriber counts — phase 2 of docs/specs/telegram_platform.md.

The only per-channel number the Bot API will give us. Views are client-API only
and reactions arrive as pushed updates, so `getChatMemberCount` is the whole of
what a poll cycle can fetch.

The integration is small because `polling/followers.py` is **duck-typed** —
`capture_followers` looks for a `get_follower_count` method by name and skips
any client without one. So a method plus one registry entry is the whole thing,
and it writes to the existing shared `account_follower_snapshots` table rather
than needing one of its own.

Verified live: the real channel returned 2, a nonexistent one returned None
(not 0), and a full cycle wrote a snapshot and refreshed the cached count.
"""
from __future__ import annotations

import pytest


class TestRegistration:
    def test_the_client_exposes_the_duck_typed_method(self):
        """capture_followers finds it by name; without it the platform is
        silently skipped, with no error anywhere."""
        from clients.tg.client import TgClient
        assert hasattr(TgClient, "get_follower_count")

    def test_telegram_is_a_follower_platform(self):
        from database.followers import FOLLOWER_PLATFORMS
        assert "tg" in FOLLOWER_PLATFORMS

    def test_the_cycle_is_registered_in_both_dispatchers(self):
        """Two maps drive polling — the server orchestrator and the manual
        trigger path. A cycle in one and not the other polls on a schedule but
        cannot be triggered by hand, or vice versa."""
        from polling.multi_account import get_poll_cycles
        assert "tg" in get_poll_cycles()
        server_src = open("server.py", encoding="utf-8").read()
        assert "run_tg_poll_cycle" in server_src


class TestFailureModes:
    """A wrong number here is worse than no number: `record_snapshot` rejects
    None, so a failed fetch is skipped — but a bogus 0 would be WRITTEN and
    would corrupt the growth series the way the 2.27.1 zero-snapshot bug
    corrupted views."""

    @pytest.mark.asyncio
    async def test_no_credentials_returns_none(self):
        from clients.tg.client import TgClient
        assert await TgClient(bot_token="", channel="").get_follower_count() is None
        assert await TgClient(bot_token="t", channel="").get_follower_count() is None

    @pytest.mark.asyncio
    async def test_an_api_refusal_returns_none_not_zero(self, monkeypatch):
        from clients.tg.client import TgClient
        c = TgClient(bot_token="t", channel="@x")

        class FakeResp:
            @staticmethod
            def json():
                return {"ok": False, "error_code": 400,
                        "description": "Bad Request: chat not found"}

        class FakeClient:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def post(self, *a, **k): return FakeResp()

        monkeypatch.setattr("httpx.AsyncClient", lambda *a, **k: FakeClient())
        got = await c.get_follower_count()
        assert got is None, "a refusal must be None — a 0 would be written as real"
        assert "chat not found" in c.last_error

    @pytest.mark.asyncio
    async def test_a_real_count_passes_through(self, monkeypatch):
        from clients.tg.client import TgClient
        c = TgClient(bot_token="t", channel="@x")

        class FakeResp:
            @staticmethod
            def json():
                return {"ok": True, "result": 1366}

        class FakeClient:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def post(self, *a, **k): return FakeResp()

        monkeypatch.setattr("httpx.AsyncClient", lambda *a, **k: FakeClient())
        assert await c.get_follower_count() == 1366


class TestCycle:
    def test_the_channel_has_no_flat_fallback(self):
        """Same rule as posting: inheriting another account's channel would
        record ITS subscriber count against this account."""
        src = open("polling/tg_poller.py", encoding="utf-8").read()
        code = "\n".join(l for l in src.splitlines()
                         if not l.lstrip().startswith("#"))
        assert 'creds.get("tg_channel", "")' in code
        assert 'or settings.get("tg_channel"' not in code

    def test_it_reports_what_it_knows(self):
        """An early version returned account_id=None and followers=None even on
        success, because it read a client attribute that did not exist."""
        src = open("polling/tg_poller.py", encoding="utf-8").read()
        assert 'result["account_id"] = account_id' in src
        assert "follower_count FROM accounts" in src
