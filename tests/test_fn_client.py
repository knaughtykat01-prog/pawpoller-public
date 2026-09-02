"""FurryNetwork client — OAuth lifecycle + submission parsing (network faked).

Locks the pieces that don't need a live account: password vs refresh grant,
the bearer header, per-character discovery + de-dup, the normalised submission
shape (views/favorites/comments/rating/link), and the tolerant search-envelope
parser. The upload flow + exact FN response shapes still need live verification
against a real account (documented in the client).
"""
import asyncio

import httpx
import pytest

from clients.fn import client as fnmod
from clients.fn.client import FnClient, _search_hits, _RATING_FROM_FN


class _Resp:
    def __init__(self, payload, status=200):
        self._p = payload
        self.status_code = status

    def json(self):
        return self._p


class _FakeClient:
    """Routes FN requests by URL and records them."""
    def __init__(self, *a, **k):
        self.posts = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def aclose(self):
        pass

    async def post(self, url, data=None, params=None, content=None, headers=None):
        self.posts.append({"url": url, "data": data})
        if url.endswith("/oauth/token"):
            return _Resp({"access_token": "AT", "refresh_token": "RT2",
                          "expires_in": 3600, "user_id": 9})
        return _Resp({"id": 999}, 200)

    async def patch(self, url, json=None, headers=None):
        return _Resp({"id": 999}, 200)

    async def get(self, url, params=None, headers=None):
        if url.endswith("/user"):
            return _Resp({"email": "me@ex.com", "name": "Me",
                          "characters": [{"name": "kit", "followers": 12}]})
        if url.endswith("/search"):
            # One page of 2 hits for character=kit (len<30 → stop).
            if (params or {}).get("from", 0) == 0:
                return _Resp([
                    {"id": 111, "title": "Wolf", "description": "a wolf",
                     "rating": 2, "views": 500, "favorites": 40, "comments": 3,
                     "images": {"original": "http://o/1", "thumbnail": "http://t/1"},
                     "tags": [{"tag": "wolf"}, "male"], "published": "2020-01-01"},
                    {"id": 112, "title": "Fox", "rating": 0, "views": 10,
                     "favorites": 1, "comments": 0, "images": {}},
                ])
            return _Resp([])
        return _Resp(None, 404)


@pytest.fixture(autouse=True)
def _fake_net(monkeypatch):
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)


def _run(coro):
    return asyncio.run(coro)


def test_password_grant_sets_tokens():
    c = FnClient(username="me@ex.com", password="pw")
    assert _run(c.login()) is True
    assert c.access_token == "AT"
    assert c.refresh_token == "RT2"
    grant = c._http().posts[0]["data"]["grant_type"]
    assert grant == "password"


def test_refresh_grant_preferred_when_token_present():
    c = FnClient(refresh_token="RT")
    assert _run(c.login()) is True
    # Present a refresh token → refresh grant, no password needed.
    assert c._http().posts[0]["data"]["grant_type"] == "refresh_token"
    assert c._http().posts[0]["data"]["client_id"] == "123"


def test_validate_session_returns_identity():
    c = FnClient(username="me@ex.com", password="pw")
    assert _run(c.validate_session()) == "me@ex.com"


def test_discovery_and_parse():
    c = FnClient(username="me@ex.com", password="pw")
    items = _run(c.get_all_post_uris())
    assert [it["post_uri"] for it in items] == ["111", "112"]
    details = _run(c.get_post_details_batch(items))
    wolf = details[0]
    assert wolf["views"] == 500 and wolf["favorites_count"] == 40 and wolf["comments_count"] == 3
    assert wolf["rating"] == "adult"                       # FN rating 2 → adult
    assert wolf["keywords"] == ["wolf", "male"]
    assert wolf["thumbnail_url"] == "http://t/1"
    assert "kit/artwork/111" in wolf["link"]               # character in the URL


def test_follower_count_sums_characters():
    c = FnClient(username="me@ex.com", password="pw")
    assert _run(c.get_follower_count()) == 12


def test_rating_map():
    assert _RATING_FROM_FN == {0: "general", 1: "mature", 2: "adult"}


def test_search_hits_envelopes():
    assert _search_hits([{"id": 1}]) == [{"id": 1}]
    assert _search_hits({"hits": [{"id": 2}]}) == [{"id": 2}]
    assert _search_hits({"hits": {"hits": [{"_source": {"id": 3}}]}}) == [{"id": 3}]
    assert _search_hits({"results": [{"id": 4}]}) == [{"id": 4}]
    assert _search_hits(None) == []
