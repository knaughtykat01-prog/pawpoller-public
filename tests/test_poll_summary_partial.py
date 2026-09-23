"""A poll that came back short must not announce itself as complete (backlog POLLPARTIAL).

The tester's Telegram read "X/Twitter Poll Complete — 513 submissions, 513 snapshots in
197.9s". The same cycle's log ends:

    WARNING TW: Rate limited (429), waiting 60s...
    ERROR   TW: Failed to fetch .../UserTweets: Client error ...
    INFO    TW: Found 513 tweets

The poller had already recorded that cycle as `partial` with "Rate-limited (429) — some
tweets may be missing"; only the Telegram summary claimed otherwise, so a truncated walk
looked like a healthy poll two days running.
"""
from __future__ import annotations

import asyncio

import pytest

from polling import telegram as tg


@pytest.fixture()
def sent(monkeypatch):
    out = []

    async def fake_send(text, **kw):
        out.append(text)

    monkeypatch.setattr(tg, "send_telegram", fake_send)
    monkeypatch.setattr(tg, "orchestrated_poll_active", False)
    monkeypatch.setattr(tg.config, "get_settings", lambda: {"telegram_poll_summaries": True})
    return out


STATS = {"submissions_found": 513, "snapshots_inserted": 513}


def test_a_clean_cycle_still_says_complete(sent):
    asyncio.run(tg.send_poll_summary("tw", STATS, 197.9))
    assert "Poll Complete" in sent[0] and "⚠" not in sent[0]


def test_a_rate_limited_cycle_says_incomplete_and_why(sent):
    asyncio.run(tg.send_poll_summary("tw", STATS, 197.9,
                                     note="Rate-limited (429) — some tweets may be missing"))
    assert "Poll Incomplete" in sent[0]
    assert "some tweets may be missing" in sent[0]


def test_the_count_is_still_reported(sent):
    asyncio.run(tg.send_poll_summary("tw", STATS, 197.9, note="Rate-limited (429)"))
    assert "513 submissions, 513 snapshots" in sent[0]


class TestThePollersPassTheirCaveat:
    def test_x_passes_the_throttled_message(self):
        src = open("polling/tw_poller.py", encoding="utf-8").read()
        assert 'send_poll_summary("tw", stats, duration, note=_msg or "")' in src

    def test_instagram_passes_its_partial_listing(self):
        src = open("polling/ig_poller.py", encoding="utf-8").read()
        assert 'send_poll_summary("ig", stats, duration,' in src
        assert "if partial else" in src.split('send_poll_summary("ig"')[1][:300]
