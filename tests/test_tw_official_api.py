"""Official X (Twitter) API v2 poll backend.

Covers the metric mapping (public_metrics → our 6), content-type + media parsing,
the is_enabled precedence in the hybrid, and the fetch/validate fallback contract
(None => caller falls back to gallery-dl/GraphQL). The X API is mocked with respx —
no token or network needed.
"""

import httpx
import pytest
import respx

from clients.tw import official_api


TOKEN_SETTINGS = {"tw_api_bearer_token": "BEARER"}

_USER = "https://api.x.com/2/users/by/username/TestHandle"
_TWEETS = "https://api.x.com/2/users/999/tweets"

_USER_OK = {"data": {"id": "999", "username": "TestHandle",
                     "public_metrics": {"followers_count": 82}}}

_TWEETS_PAGE = {
    "data": [
        {"id": "111", "text": "hello world", "created_at": "2023-09-01T12:00:00.000Z",
         "public_metrics": {"retweet_count": 2, "reply_count": 1, "like_count": 5,
                            "quote_count": 0, "bookmark_count": 3, "impression_count": 100},
         "entities": {"hashtags": [{"tag": "art"}, {"tag": "furry"}]},
         "attachments": {"media_keys": ["mk1"]}},
        {"id": "222", "text": "a reply", "created_at": "2023-09-02T08:30:00.000Z",
         "public_metrics": {"like_count": 1, "impression_count": 10},
         "referenced_tweets": [{"type": "replied_to", "id": "999"}]},
    ],
    "includes": {"media": [{"media_key": "mk1", "type": "photo",
                            "url": "https://pbs.twimg.com/media/AAA.jpg"}]},
    "meta": {"result_count": 2},
}


@pytest.fixture(autouse=True)
def _clear_follower_cache():
    official_api._LAST_FOLLOWERS.clear()
    yield
    official_api._LAST_FOLLOWERS.clear()


# ── Pure parsing ────────────────────────────────────────────────────────────

def test_build_detail_maps_public_metrics():
    d = official_api._build_detail(_TWEETS_PAGE["data"][0], "TestHandle",
                                   {"mk1": _TWEETS_PAGE["includes"]["media"][0]})
    assert d["tweet_id"] == "111"
    assert d["views"] == 100 and d["likes"] == 5 and d["retweets"] == 2
    assert d["replies"] == 1 and d["quotes"] == 0 and d["bookmarks"] == 3
    assert d["keywords"] == ["art", "furry"]
    assert d["media_urls"] == ["https://pbs.twimg.com/media/AAA.jpg"]
    assert d["thumbnail_url"] == "https://pbs.twimg.com/media/AAA.jpg"
    assert d["content_type"] == "tweet"
    assert d["posted_at"] == "2023-09-01 12:00:00"
    assert d["link"] == "https://x.com/TestHandle/status/111"


def test_build_detail_content_types():
    def _ct(refs):
        t = {"id": "1", "text": "x", "public_metrics": {}, "referenced_tweets": refs}
        return official_api._build_detail(t, "h", {})["content_type"]
    assert _ct([{"type": "replied_to", "id": "9"}]) == "reply"
    assert _ct([{"type": "quoted", "id": "9"}]) == "quote"
    assert _ct([{"type": "retweeted", "id": "9"}]) == "retweet"
    assert _ct([]) == "tweet"


def test_normalize_date_iso():
    assert official_api._normalize_date("2023-09-01T12:00:00.000Z") == "2023-09-01 12:00:00"
    assert official_api._normalize_date("2023-09-01T12:00:00+00:00") == "2023-09-01 12:00:00"
    assert official_api._normalize_date("") == ""
    assert official_api._normalize_date(None) == ""


# ── is_enabled precedence ───────────────────────────────────────────────────

def test_is_enabled_precedence():
    assert official_api.is_enabled({}) is False                     # no token
    assert official_api.is_enabled(TOKEN_SETTINGS) is True           # token + auto
    assert official_api.is_enabled({**TOKEN_SETTINGS, "tw_polling_backend": "official"}) is True
    assert official_api.is_enabled({**TOKEN_SETTINGS, "tw_polling_backend": "graphql"}) is False
    assert official_api.is_enabled({**TOKEN_SETTINGS, "tw_polling_backend": "gallerydl"}) is False


# ── fetch_tweets (mocked X API) ─────────────────────────────────────────────

@pytest.mark.asyncio
@respx.mock
async def test_fetch_tweets_full_pipeline():
    respx.get(url__startswith=_USER).mock(return_value=httpx.Response(200, json=_USER_OK))
    respx.get(url__startswith=_TWEETS).mock(return_value=httpx.Response(200, json=_TWEETS_PAGE))

    out = await official_api.fetch_tweets("BEARER", "@TestHandle", settings=TOKEN_SETTINGS)
    assert out is not None and len(out) == 2
    assert out[0]["views"] == 100 and out[0]["likes"] == 5
    assert out[1]["content_type"] == "reply" and out[1]["media_urls"] == []
    # Follower count captured from the user lookup, cached for get_follower_count().
    assert official_api._LAST_FOLLOWERS["testhandle"] == 82


@pytest.mark.asyncio
@respx.mock
async def test_fetch_tweets_bad_token_falls_back():
    # 401 on user resolve → None so the caller falls back to a scraper.
    respx.get(url__startswith=_USER).mock(return_value=httpx.Response(401, json={"title": "Unauthorized"}))
    out = await official_api.fetch_tweets("BADTOKEN", "TestHandle", settings=TOKEN_SETTINGS)
    assert out is None


@pytest.mark.asyncio
async def test_fetch_tweets_none_when_disabled():
    out = await official_api.fetch_tweets("BEARER", "TestHandle", settings={})  # no token → disabled
    assert out is None


# ── validate ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
@respx.mock
async def test_validate_ok():
    respx.get(url__startswith=_USER).mock(return_value=httpx.Response(200, json=_USER_OK))
    assert await official_api.validate("BEARER", "TestHandle", settings=TOKEN_SETTINGS) is True


@pytest.mark.asyncio
@respx.mock
async def test_validate_bad_token():
    respx.get(url__startswith=_USER).mock(return_value=httpx.Response(403, json={"title": "Forbidden"}))
    assert await official_api.validate("BEARER", "TestHandle", settings=TOKEN_SETTINGS) is False


@pytest.mark.asyncio
async def test_validate_none_when_disabled():
    assert await official_api.validate("BEARER", "TestHandle", settings={}) is None


# ── get_follower_count ──────────────────────────────────────────────────────

@pytest.mark.asyncio
@respx.mock
async def test_follower_count_uses_cache_then_lookup():
    # Direct lookup (cold cache).
    route = respx.get(url__startswith=_USER).mock(return_value=httpx.Response(200, json=_USER_OK))
    fc = await official_api.get_follower_count("BEARER", "TestHandle", settings=TOKEN_SETTINGS)
    assert fc == 82 and route.call_count == 1
    # Second call is served from the warmed cache — no extra billed request.
    fc2 = await official_api.get_follower_count("BEARER", "TestHandle", settings=TOKEN_SETTINGS)
    assert fc2 == 82 and route.call_count == 1
