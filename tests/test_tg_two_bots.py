"""Telegram: two bots, two jobs (4.8.0).

A six-hour digest was found posted in a public channel. One bot did both
jobs, and the notification connect flow — which scans the bot's recent
updates for "any chat" — picked the channel's chat from a my_chat_member
event. So: notifications go to a PRIVATE chat only (connect, test, every
sender); channel posting needs its own bot (no fallback to the notification
bot anywhere); and each channel bot reads its own reaction updates, which
also closes the channel-stats gap.
"""
from __future__ import annotations

import asyncio
import json
import logging

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import config
from polling import telegram as tg
from polling import telegram_bot as bot

_RealAsyncClient = httpx.AsyncClient   # monkeypatching httpx.AsyncClient below must not recurse into itself


@pytest.fixture()
def settings(monkeypatch):
    state: dict = {}
    monkeypatch.setattr(config, "get_settings", lambda: dict(state))
    monkeypatch.setattr(config, "save_settings", lambda d: state.update(d))
    return state


@pytest.fixture()
def api(settings, monkeypatch):
    from routes import api as api_mod
    app = FastAPI()
    app.include_router(api_mod.router)
    calls: list[httpx.Request] = []
    responder = {"fn": None}

    def factory(*a, **k):
        async def h(req):
            calls.append(req)
            return responder["fn"](req) if responder["fn"] else httpx.Response(200, json={"ok": True, "result": []})
        return _RealAsyncClient(transport=httpx.MockTransport(h))
    monkeypatch.setattr(api_mod.httpx, "AsyncClient", factory)
    return TestClient(app), settings, calls, responder


def _updates(*items):
    return httpx.Response(200, json={"ok": True, "result": list(items)})


# ── notifications: private chats only ────────────────────────────────────────

class TestConnectPicksThePrivateChat:
    def test_channel_events_are_skipped_the_persons_chat_is_kept(self, api):
        c, state, calls, r = api
        r["fn"] = lambda req: _updates(
            {"update_id": 1, "my_chat_member": {"chat": {"id": -1001234567, "type": "channel", "title": "club"}}},
            {"update_id": 2, "channel_post": {"chat": {"id": -1001234567, "type": "channel"}, "text": "hi"}},
            {"update_id": 3, "message": {"chat": {"id": 55501, "type": "private"}, "text": "/start"}},
        )
        res = c.post("/api/settings/telegram", json={"bot_token": "111:notif"})
        assert res.status_code == 200, res.text
        assert state["telegram_chat_id"] == "55501" and state["telegram_enabled"] is True

    def test_only_channel_events_is_not_connected(self, api):
        c, state, calls, r = api
        r["fn"] = lambda req: _updates(
            {"update_id": 1, "my_chat_member": {"chat": {"id": -1001234567, "type": "channel"}}})
        res = c.post("/api/settings/telegram", json={"bot_token": "111:notif"})
        assert res.status_code == 404
        assert "your own Telegram account" in res.json()["detail"] and "channel" in res.json()["detail"]
        assert "telegram_chat_id" not in state

    def test_the_posting_bot_is_refused_as_the_notification_bot(self, api):
        c, state, calls, r = api
        state["tg_bot_token"] = "222:chan"
        res = c.post("/api/settings/telegram", json={"bot_token": "222:chan"})
        assert res.status_code == 400 and "channel-posting bot" in res.json()["detail"]
        assert calls == []


class TestTestAndStatus:
    def test_test_refuses_a_channel_chat(self, api):
        c, state, calls, r = api
        state.update({"telegram_bot_token": "111:notif", "telegram_chat_id": "-1001234567"})
        res = c.post("/api/settings/telegram/test")
        assert res.status_code == 400 and "public" in res.json()["detail"]
        assert calls == [], "nothing may be sent to a channel"

    def test_test_sends_to_a_private_chat(self, api):
        c, state, calls, r = api
        state.update({"telegram_bot_token": "111:notif", "telegram_chat_id": "55501"})
        r["fn"] = lambda req: httpx.Response(200, json={"ok": True})
        assert c.post("/api/settings/telegram/test").status_code == 200
        assert json.loads(calls[0].content)["chat_id"] == "55501"

    def test_status_says_whether_the_chat_is_private(self, api):
        c, state, calls, r = api
        state.update({"telegram_bot_token": "111:notif", "telegram_chat_id": "-1001234567"})
        assert c.get("/api/settings/telegram").json()["chat_is_private"] is False
        state["telegram_chat_id"] = "55501"
        assert c.get("/api/settings/telegram").json()["chat_is_private"] is True


class TestEverySenderIsGated:
    def test_notification_target(self, caplog):
        assert tg.notification_target({"telegram_enabled": False, "telegram_bot_token": "t", "telegram_chat_id": "1"}) is None
        assert tg.notification_target({"telegram_enabled": True, "telegram_bot_token": "t"}) is None
        assert tg.notification_target({"telegram_enabled": True, "telegram_bot_token": "t", "telegram_chat_id": "55501"}) == ("t", "55501")
        tg._warned_public_chat.clear()
        with caplog.at_level(logging.WARNING):
            assert tg.notification_target({"telegram_enabled": True, "telegram_bot_token": "t", "telegram_chat_id": "-100777"}) is None
            assert tg.notification_target({"telegram_enabled": True, "telegram_bot_token": "t", "telegram_chat_id": "-100777"}) is None
        warns = [m for m in caplog.messages if "channel or group" in m]
        assert len(warns) == 1, "warned once per chat id, not once per digest"

    def test_is_private_chat(self):
        assert tg.is_private_chat("55501") and tg.is_private_chat(55501)
        assert not tg.is_private_chat("-1001234567") and not tg.is_private_chat("-42") and not tg.is_private_chat("")

    def test_send_telegram_refuses_a_channel(self, settings, monkeypatch):
        settings.update({"telegram_enabled": True, "telegram_bot_token": "t", "telegram_chat_id": "-1001234567"})
        sent = []

        def factory(*a, **k):
            async def h(req):
                sent.append(req)
                return httpx.Response(200, json={"ok": True})
            return _RealAsyncClient(transport=httpx.MockTransport(h))
        monkeypatch.setattr(tg.httpx, "AsyncClient", factory)
        assert asyncio.run(tg.send_telegram("<b>digest</b>")) is False and sent == []
        settings["telegram_chat_id"] = "55501"
        assert asyncio.run(tg.send_telegram("<b>digest</b>")) is True and len(sent) == 1

    def test_the_direct_senders_use_the_same_gate(self):
        for path in ("polling/fa_poller.py", "polling/notifications.py", "polling/poller.py"):
            src = open(path, encoding="utf-8").read()
            assert "notification_target" in src, path
            assert 'chat_id = settings.get("telegram_chat_id")' not in src, path


# ── channel posting: its own bot ─────────────────────────────────────────────

class TestNoFallbackToTheNotificationBot:
    def test_registry_and_session_check(self):
        from database import accounts
        from polling import session_check
        only_notif = {"telegram_bot_token": "111:notif", "tg_channel": "@club"}
        assert accounts.DEFAULT_CRED_CHECKS["tg"](only_notif) is False
        assert accounts.DEFAULT_CRED_CHECKS["tg"]({"tg_bot_token": "222:chan", "tg_channel": "@club"}) is True
        src = open("database/accounts.py", encoding="utf-8").read()
        assert '"tg": lambda s: bool(s.get("tg_bot_token") and s.get("tg_channel"))' in src
        assert session_check.__dict__  # module imports cleanly
        sc = open("polling/session_check.py", encoding="utf-8").read()
        assert 'or s.get("telegram_bot_token")' not in sc

    def test_the_poster_says_what_it_needs(self, settings, monkeypatch):
        from posting.platforms.telegram import TelegramPoster
        from posting.platforms.base import StoryUploadPackage
        p = TelegramPoster()
        monkeypatch.setattr(TelegramPoster, "_resolve_creds", lambda self, plat, s: {"tg_channel": "@club"})
        settings.update({"telegram_bot_token": "111:notif"})
        pkg = StoryUploadPackage(story_name="Sample", chapter_index=0, chapter_title="", platform="tg",
                                 title="Sample", description="d", tags=[], rating="general",
                                 file_path=__file__, file_type="png", word_count=0)
        r = asyncio.run(p.post(pkg))
        assert r.success is False and "needs its own bot token" in r.error

    def test_no_reader_of_the_notification_token_remains_in_the_posting_path(self):
        for path in ("posting/platforms/telegram.py", "posting/post_publisher.py", "polling/tg_poller.py"):
            src = open(path, encoding="utf-8").read()
            live = [l for l in src.splitlines() if "telegram_bot_token" in l and not l.strip().startswith("#")]
            assert not live, (path, live)

    def test_saving_the_notification_token_as_the_posting_bot_is_refused(self, api):
        c, state, calls, r = api
        state["telegram_bot_token"] = "111:notif"
        res = c.post("/api/settings/telegram/channel", json={"channel": "@club", "bot_token": "111:notif"})
        assert res.status_code == 400 and "needs its own" in res.json()["detail"]
        res = c.post("/api/settings/telegram/channel", json={"channel": "@club", "bot_token": "222:chan"})
        assert res.status_code == 200 and state["tg_bot_token"] == "222:chan"

    def test_channel_status_and_detect_need_the_posting_bot(self, api):
        c, state, calls, r = api
        state.update({"telegram_bot_token": "111:notif", "tg_channel": "@club"})
        st = c.get("/api/settings/telegram/channel").json()
        assert st["configured"] is False and st["needs_own_token"] is True and st["uses_notification_bot"] is False
        res = c.post("/api/settings/telegram/channel/detect", json={})
        assert res.status_code == 400 and "its own bot" in res.json()["detail"]
        res = c.post("/api/settings/telegram/channel/test", json={"channel": "@club"})
        assert res.status_code == 400 and "its own bot" in res.json()["detail"]


# ── the update loop: one reader per bot ──────────────────────────────────────

class TestChannelBotReaders:
    def test_channel_bot_tokens_excludes_the_notification_bot_and_dedups(self):
        s = {"telegram_bot_token": "111:notif", "tg_bot_token": "222:chan",
             "acct_7_tg_bot_token": "222:chan", "acct_8_tg_bot_token": "333:other",
             "acct_9_tg_bot_token": "111:notif", "acct_10_tg_bot_token": ""}
        assert bot.channel_bot_tokens(s) == ["222:chan", "333:other"]
        assert bot.channel_bot_tokens({"telegram_bot_token": "111:notif"}) == []

    def test_poll_updates_keeps_an_offset_per_token(self, monkeypatch):
        seen = []

        def factory(*a, **k):
            async def h(req):
                seen.append((req.url.path.split("/bot")[1].split("/")[0], dict(req.url.params)))
                tok = req.url.path.split("/bot")[1].split("/")[0]
                ids = {"222:chan": [5, 6], "333:other": [40]}[tok]
                return httpx.Response(200, json={"ok": True, "result": [{"update_id": i} for i in ids]})
            return _RealAsyncClient(transport=httpx.MockTransport(h))
        monkeypatch.setattr(bot.httpx, "AsyncClient", factory)
        bot._last_update_ids.clear()
        asyncio.run(bot._poll_updates("222:chan", bot._REACTIONS_ONLY))
        asyncio.run(bot._poll_updates("333:other", bot._REACTIONS_ONLY))
        asyncio.run(bot._poll_updates("222:chan", bot._REACTIONS_ONLY))
        assert [(t, p["offset"]) for t, p in seen] == [("222:chan", "1"), ("333:other", "1"), ("222:chan", "7")]
        assert all(p["allowed_updates"] == '["message_reaction_count"]' for _, p in seen)
        assert bot._last_update_ids == {"222:chan": 6, "333:other": 40}

    def test_a_409_backs_off_only_that_token(self, monkeypatch):
        def factory(*a, **k):
            async def h(req):
                return httpx.Response(409, json={"ok": False})
            return _RealAsyncClient(transport=httpx.MockTransport(h))
        monkeypatch.setattr(bot.httpx, "AsyncClient", factory)
        bot._conflict_backoff.clear()
        asyncio.run(bot._poll_updates("222:chan"))
        asyncio.run(bot._poll_updates("222:chan"))
        assert bot._conflict_backoff == {"222:chan": 60}

    def test_channel_bot_records_reactions_and_answers_nothing(self, monkeypatch):
        recorded, handled = [], []
        bot._flushed.add("222:chan")

        async def poll(token, allowed=bot._ALLOWED_UPDATES):
            assert token == "222:chan" and allowed == bot._REACTIONS_ONLY
            return [{"update_id": 9, "message_reaction_count": {"chat": {"id": -100123}, "message_id": 14, "reactions": []}},
                    {"update_id": 10, "message": {"chat": {"id": -100123}, "text": "/help"}}]
        monkeypatch.setattr(bot, "_poll_updates", poll)
        monkeypatch.setattr(bot, "_record_reaction_update", lambda p: recorded.append(p["message_id"]))

        async def handle(*a):
            handled.append(a)
        monkeypatch.setattr(bot, "_handle_message", handle)
        asyncio.run(bot._poll_channel_bot("222:chan"))
        assert recorded == [14] and handled == []

    def test_notification_bot_ignores_commands_when_its_chat_is_public(self, monkeypatch, caplog):
        handled = []
        bot._flushed.add("111:notif")
        bot._warned_public_commands.clear()

        async def poll(token, allowed=bot._ALLOWED_UPDATES):
            return [{"update_id": 1, "message": {"chat": {"id": -100123}, "text": "/stats"}}]
        monkeypatch.setattr(bot, "_poll_updates", poll)

        async def handle(*a):
            handled.append(a)
        monkeypatch.setattr(bot, "_handle_message", handle)
        with caplog.at_level(logging.WARNING):
            asyncio.run(bot._poll_notification_bot("111:notif", "-100123"))
        assert handled == [] and any("commands are disabled" in m for m in caplog.messages)
        asyncio.run(bot._poll_notification_bot("111:notif", "-100123"))
        assert sum("commands are disabled" in m for m in caplog.messages) == 1

    def test_notification_bot_answers_its_private_chat(self, monkeypatch):
        handled = []
        bot._flushed.add("111:notif")

        async def poll(token, allowed=bot._ALLOWED_UPDATES):
            return [{"update_id": 1, "message": {"chat": {"id": 55501}, "text": "/stats"}},
                    {"update_id": 2, "message": {"chat": {"id": 999}, "text": "/stats"}}]
        monkeypatch.setattr(bot, "_poll_updates", poll)

        async def handle(token, chat_id, text):
            handled.append((chat_id, text))
        monkeypatch.setattr(bot, "_handle_message", handle)
        asyncio.run(bot._poll_notification_bot("111:notif", "55501"))
        assert handled == [("55501", "/stats")]
