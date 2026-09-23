"""A failed page must not read as "that is all your posts" (backlog IGSTUCK).

From a tester's log, twice in one day:

    ERROR clients.ig.client: IG: Failed to fetch …/media?…&after=QVFI…
    INFO  clients.ig.client: IG: Found 100 media for <account>

The media walk ended on the failed page and reported the partial count as the whole
gallery, so their library sat at the same number every cycle while every poll said it
had succeeded. The page is now retried, and a listing that still ends early says so.
"""
from __future__ import annotations

import asyncio

import pytest

from clients.ig.client import IgClient


def _page(ids, nxt=None):
    data = {"data": [{"id": str(i)} for i in ids]}
    if nxt:
        data["paging"] = {"next": nxt}
    return data


@pytest.fixture()
def client(monkeypatch):
    c = IgClient(access_token="t", user_id="123")
    monkeypatch.setattr(c, "ensure_logged_in", lambda: _true())
    monkeypatch.setattr(asyncio, "sleep", lambda *_a, **_k: _none())
    return c


async def _true():
    return True


async def _none():
    return None


def _walk(c, pages):
    """pages: list of page objects or None (a page that will not load)."""
    calls = {"n": 0}

    async def fake_get(url, params=None):
        i = calls["n"]
        calls["n"] += 1
        return pages[i] if i < len(pages) else None

    c._get_json = fake_get
    return asyncio.run(c.get_all_post_uris()), calls


def test_a_whole_gallery_is_not_flagged(client):
    posts, _ = _walk(client, [_page([1, 2], "p2"), _page([3, 4])])
    assert [p["post_uri"] for p in posts] == ["1", "2", "3", "4"]
    assert client.partial_fetch is False


def test_a_page_that_fails_once_is_retried(client):
    """Most of these are a momentary 500 from Meta — one retry recovers the walk."""
    posts, calls = _walk(client, [_page([1], "p2"), None, _page([2])])
    assert [p["post_uri"] for p in posts] == ["1", "2"]
    assert client.partial_fetch is False
    assert calls["n"] == 3                      # page 1, the failure, the retry


def test_a_page_that_keeps_failing_marks_the_listing_partial(client):
    posts, _ = _walk(client, [_page([1, 2], "p2"), None, None])
    assert [p["post_uri"] for p in posts] == ["1", "2"]
    assert client.partial_fetch is True, "a short listing must not pass as the whole gallery"


def test_an_account_with_no_posts_is_not_partial(client):
    """Nothing fetched is a different thing from a walk that broke part-way."""
    posts, _ = _walk(client, [None, None])
    assert posts == []
    assert client.partial_fetch is False


def test_the_poller_says_so(client):
    src = open("polling/ig_poller.py", encoding="utf-8").read()
    assert 'getattr(client, "partial_fetch", False)' in src
    # The "Done" line the user sees at the end of a poll carries the caveat.
    done = src[src.rindex('_update_ig_progress("complete"'):][:600]
    assert "partial" in done and "if partial else" in done
