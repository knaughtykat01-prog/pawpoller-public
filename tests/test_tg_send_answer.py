"""TGSEND (4.79.2): a Telegram send reads Telegram's answer.

Telegram answers every sendMessage with {"ok": ...}. It was never read, so a refused notification
(too long, unreadable markup, a blocked bot) counted as sent and left nothing in the log.
"""
import asyncio
import logging

import httpx

from polling import notifications, telegram_bot

_RealAsyncClient = httpx.AsyncClient


def _answer(monkeypatch, handler):
    sent = []

    def factory(*a, **k):
        async def h(req):
            sent.append(req)
            return handler(req, len(sent))
        return _RealAsyncClient(transport=httpx.MockTransport(h))
    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return sent


def _send(text):
    return asyncio.run(notifications.send_telegram("t", "55501", text, log_label="FA"))


def test_a_refused_message_is_not_sent_and_says_why(monkeypatch, caplog):
    _answer(monkeypatch, lambda r, n: httpx.Response(403, json={"ok": False, "description": "Forbidden: bot was blocked by the user"}))
    with caplog.at_level(logging.WARNING):
        assert _send("<b>hi</b>") is False
    assert any("blocked by the user" in m for m in caplog.messages)


def test_a_taken_message_is_sent(monkeypatch):
    sent = _answer(monkeypatch, lambda r, n: httpx.Response(200, json={"ok": True}))
    assert _send("<b>hi</b>") is True and len(sent) == 1


def test_unreadable_markup_goes_again_as_plain_text(monkeypatch):
    def h(r, n):
        if n == 1:
            return httpx.Response(400, json={"ok": False, "description": "Bad Request: can't parse entities"})
        return httpx.Response(200, json={"ok": True})
    sent = _answer(monkeypatch, h)
    assert _send("<b>New comment on a <3 b</b>") is True
    body = sent[1].content.decode()
    assert "parse_mode" not in body and "<b>" not in body


def test_an_over_long_message_is_trimmed_on_a_line(monkeypatch):
    sent = _answer(monkeypatch, lambda r, n: httpx.Response(200, json={"ok": True}))
    assert _send("\n".join(f"<b>line {i}</b>" for i in range(1000))) is True
    import json
    text = json.loads(sent[0].content)["text"]
    assert len(text) <= notifications.TELEGRAM_MESSAGE_LIMIT and text.endswith("(trimmed)")
    assert text.split("\n")[-3].endswith("</b>")


def test_a_refused_getupdates_backs_off_and_says_why(monkeypatch, caplog):
    _answer(monkeypatch, lambda r, n: httpx.Response(401, json={"ok": False, "description": "Unauthorized"}))
    telegram_bot._conflict_backoff.pop("t", None)
    with caplog.at_level(logging.WARNING):
        assert asyncio.run(telegram_bot._poll_updates("t")) == []
    assert telegram_bot._conflict_backoff["t"] == 30
    assert any("Unauthorized" in m for m in caplog.messages)
    telegram_bot._conflict_backoff.pop("t", None)


def test_a_long_poll_timeout_is_quiet(monkeypatch, caplog):
    def h(r, n):
        raise httpx.ReadTimeout("")
    _answer(monkeypatch, h)
    with caplog.at_level(logging.WARNING):
        assert asyncio.run(telegram_bot._poll_updates("t")) == []
    assert not caplog.messages
