"""A cut-short X timeline walk resumes where it stopped (backlog TWRESUME).

X's per-IP budget runs out part-way down a long timeline. Starting from the top every
cycle re-read the same first pages for ever: an account stopped at the identical count
two cycles running, each ending on a 429, so the tail of the timeline was never read.
The client now reports the cursor it died on and accepts one to start from.
"""
from __future__ import annotations

import asyncio

import pytest

from clients.tw.client import TWClient


def _timeline(entries, cursor=None):
    """The shape _get_all_tweets_graphql walks: entries + an optional bottom cursor."""
    items = [{"entryId": f"tweet-{i}", "content": {"entryType": "TimelineTimelineItem",
              "itemContent": {"tweet_results": {"result": {
                  "__typename": "Tweet", "rest_id": str(i),
                  "legacy": {"full_text": f"t{i}", "created_at": "Mon Sep 22 00:00:00 +0000 2026"},
              }}}}} for i in entries]
    if cursor:
        items.append({"entryId": "cursor-bottom-0",
                      "content": {"entryType": "TimelineTimelineCursor",
                                  "cursorType": "Bottom", "value": cursor}})
    return {"data": {"user": {"result": {"timeline_v2": {"timeline": {
        "instructions": [{"type": "TimelineAddEntries", "entries": items}]}}}}}}


@pytest.fixture()
def client(monkeypatch):
    c = TWClient(auth_token="a", ct0="c", target_user="someone")

    async def _uid():
        return "42"

    monkeypatch.setattr(c, "_get_user_id", _uid)
    monkeypatch.setattr(asyncio, "sleep", lambda *_a, **_k: _none())
    return c


async def _none():
    return None


def _walk(c, pages, start=""):
    seen_cursors = []

    async def fake_get(url, params=None):
        import json as _json
        variables = _json.loads(params["variables"])
        seen_cursors.append(variables.get("cursor"))
        i = len(seen_cursors) - 1
        return pages[i] if i < len(pages) else None

    c._get_json = fake_get
    tweets = asyncio.run(c._get_all_tweets_graphql(start))
    return tweets, seen_cursors


def test_a_complete_walk_leaves_no_resume_point(client):
    tweets, _ = _walk(client, [_timeline([1, 2], "c1"), _timeline([3, 4])])
    assert len(tweets) == 4
    assert client.stopped_cursor == ""


def test_a_walk_cut_short_remembers_where(client):
    """Page two dies — the cursor that would have fetched it is the resume point."""
    tweets, _ = _walk(client, [_timeline([1, 2], "c1"), None])
    assert len(tweets) == 2
    assert client.stopped_cursor == "c1"


def test_the_next_cycle_starts_from_there(client):
    tweets, cursors = _walk(client, [_timeline([3, 4])], start="c1")
    assert cursors[0] == "c1", "the first request must carry the saved cursor"
    assert len(tweets) == 2
    assert client.stopped_cursor == ""      # reached the end: nothing to resume


def test_a_resumed_walk_that_is_cut_short_again_moves_deeper(client):
    tweets, _ = _walk(client, [_timeline([3, 4], "c2"), None], start="c1")
    assert client.stopped_cursor == "c2", "progress through the tail, not a repeat of c1"


class TestThePollerStoresIt:
    def test_it_reads_and_writes_a_per_account_key(self):
        src = open("polling/tw_poller.py", encoding="utf-8").read()
        assert 'config.account_setting_key(account_id, "tw_resume_cursor", is_default)' in src
        assert "client.get_all_tweets(_resume_from)" in src
        assert "config.save_settings({_cursor_key: _stopped_at})" in src
