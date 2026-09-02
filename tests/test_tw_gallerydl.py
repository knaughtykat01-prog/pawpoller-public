"""gallery-dl subprocess backend for the X/Twitter poll path.

Covers the pure parsing of ``gallery-dl -j`` output into TWClient's detail-dict
shape, the discovery/enable logic, the Netscape cookie writer, and the
fetch/validate fallback contract (None => caller falls back to GraphQL). No real
gallery-dl binary is required — the subprocess boundary is monkeypatched.
"""

import json

import pytest

from clients.tw import gallerydl


# ── Sample gallery-dl -j output ─────────────────────────────────────────────
# Media file entries are [type, url, kwdict]; a text-only tweet is [type, kwdict].
# The tweet metadata is always the LAST element, which is what the parser keys on.
_MEDIA_KW = {
    "tweet_id": 1700000000000000000,
    "content": "hello world",
    "author": {"name": "TestHandle", "followers_count": 42},
    "date": "2023-09-01 12:00:00",
    "favorite_count": 5, "retweet_count": 2, "reply_count": 1,
    "quote_count": 0, "bookmark_count": 3, "view_count": 100,
    "hashtags": ["art", "furry"], "extension": "jpg",
}
_TEXT_KW = {
    "tweet_id": 1700000000000000001,
    "content": "just some text, no media",
    "author": {"name": "TestHandle"},
    "date": "2023-09-02T08:30:00.000Z",
    "favorite_count": 1, "retweet_count": 0, "reply_count": 0,
    "quote_count": 0, "bookmark_count": 0, "view_count": 10,
}
SAMPLE = json.dumps([
    [3, "https://pbs.twimg.com/media/AAA.jpg", _MEDIA_KW],
    [2, _TEXT_KW],
])


# ── Parsing ─────────────────────────────────────────────────────────────────

def test_parse_media_and_text_tweets():
    tweets = gallerydl._parse_dump_json(SAMPLE, "TestHandle")
    assert len(tweets) == 2
    media, text = tweets

    assert media["tweet_id"] == "1700000000000000000"
    assert media["likes"] == 5 and media["retweets"] == 2 and media["replies"] == 1
    assert media["bookmarks"] == 3 and media["views"] == 100
    assert media["title"].startswith("hello world")
    assert media["description"] == "hello world"
    assert media["keywords"] == ["art", "furry"]
    assert media["media_urls"] == ["https://pbs.twimg.com/media/AAA.jpg"]
    assert media["thumbnail_url"] == "https://pbs.twimg.com/media/AAA.jpg"
    assert media["link"] == "https://x.com/TestHandle/status/1700000000000000000"
    assert media["content_type"] == "tweet"
    assert media["posted_at"] == "2023-09-01 12:00:00"

    # Text-only tweet: no media, ISO date normalised to a space-separated string.
    assert text["media_urls"] == [] and text["thumbnail_url"] == ""
    assert text["views"] == 10 and text["likes"] == 1
    assert text["posted_at"] == "2023-09-02 08:30:00"


def test_parse_multi_image_collapses_to_one_tweet():
    raw = json.dumps([
        [3, "https://pbs.twimg.com/media/ONE.jpg", dict(_MEDIA_KW)],
        [3, "https://pbs.twimg.com/media/TWO.png", dict(_MEDIA_KW)],
    ])
    tweets = gallerydl._parse_dump_json(raw, "TestHandle")
    assert len(tweets) == 1
    assert tweets[0]["media_urls"] == [
        "https://pbs.twimg.com/media/ONE.jpg",
        "https://pbs.twimg.com/media/TWO.png",
    ]
    assert tweets[0]["thumbnail_url"] == "https://pbs.twimg.com/media/ONE.jpg"


def test_parse_skips_non_image_media():
    kw = dict(_MEDIA_KW)
    kw["extension"] = "mp4"
    raw = json.dumps([[3, "https://video.twimg.com/ext_tw_video/x.mp4", kw]])
    tweets = gallerydl._parse_dump_json(raw, "TestHandle")
    assert len(tweets) == 1
    assert tweets[0]["media_urls"] == []  # video not importable as artwork


def test_parse_content_types():
    def _ct(extra):
        kw = {"tweet_id": 1, "content": "x", "author": {"name": "h"}, **extra}
        return gallerydl._parse_dump_json(json.dumps([[2, kw]]), "h")[0]["content_type"]

    assert _ct({"reply_id": 9}) == "reply"
    assert _ct({"retweet_id": 9}) == "retweet"
    assert _ct({"quote_id": 9}) == "quote"
    assert _ct({}) == "tweet"


def test_parse_garbage_returns_empty():
    assert gallerydl._parse_dump_json("", "h") == []
    assert gallerydl._parse_dump_json("not json", "h") == []
    assert gallerydl._parse_dump_json("{}", "h") == []          # object, not array
    assert gallerydl._parse_dump_json("[1, 2, 3]", "h") == []    # no dicts


def test_parse_falls_back_to_snowflake_date_when_missing():
    kw = {"tweet_id": 1445919810076827648, "content": "hi", "author": {"name": "h"}}
    d = gallerydl._parse_dump_json(json.dumps([[2, kw]]), "h")[0]
    assert d["posted_at"] and d["posted_at"][:4].isdigit()


def test_normalize_date_forms():
    assert gallerydl._normalize_date("2023-09-01 12:00:00") == "2023-09-01 12:00:00"
    assert gallerydl._normalize_date("2023-09-01T12:00:00.000Z") == "2023-09-01 12:00:00"
    assert gallerydl._normalize_date("2023-09-01T12:00:00+00:00") == "2023-09-01 12:00:00"
    assert gallerydl._normalize_date("") == ""
    assert gallerydl._normalize_date(None) == ""


# ── Cookie jar ──────────────────────────────────────────────────────────────

def test_write_cookies_netscape_format(tmp_path):
    path = tmp_path / "cookies.txt"
    gallerydl._write_cookies("AUTHVAL", "CT0VAL", str(path))
    content = path.read_text(encoding="utf-8")
    assert content.startswith("# Netscape HTTP Cookie File")
    assert "\tauth_token\tAUTHVAL" in content
    assert "\tct0\tCT0VAL" in content
    for line in content.splitlines()[1:]:
        assert line.startswith(".x.com\t")


# ── Discovery / enable ──────────────────────────────────────────────────────

def test_find_gallerydl_none_when_absent(monkeypatch):
    monkeypatch.setattr(gallerydl.shutil, "which", lambda *_a, **_k: None)
    assert gallerydl.find_gallerydl({}) is None


def test_find_gallerydl_explicit_bad_path_falls_through(monkeypatch, tmp_path):
    monkeypatch.setattr(gallerydl.shutil, "which", lambda *_a, **_k: "/usr/bin/gallery-dl")
    # A non-existent explicit path should fall through to the PATH lookup.
    got = gallerydl.find_gallerydl({"tw_gallerydl_path": str(tmp_path / "nope")})
    assert got == "/usr/bin/gallery-dl"


def test_is_enabled_backend_graphql_forces_off(monkeypatch):
    monkeypatch.setattr(gallerydl.shutil, "which", lambda *_a, **_k: "/usr/bin/gallery-dl")
    assert gallerydl.is_enabled({"tw_polling_backend": "graphql"}) is False


def test_is_enabled_backend_official_forces_off(monkeypatch):
    # "official" forces the paid API first, so gallery-dl must stand down even
    # when the binary is present (otherwise it would preempt the chosen backend).
    monkeypatch.setattr(gallerydl.shutil, "which", lambda *_a, **_k: "/usr/bin/gallery-dl")
    assert gallerydl.is_enabled({"tw_polling_backend": "official"}) is False


def test_is_enabled_auto_uses_gallerydl_when_present(monkeypatch):
    monkeypatch.setattr(gallerydl.shutil, "which", lambda *_a, **_k: "/usr/bin/gallery-dl")
    assert gallerydl.is_enabled({"tw_polling_backend": "auto"}) is True
    assert gallerydl.is_enabled({}) is True  # auto is the default


# ── fetch/validate fallback contract ────────────────────────────────────────

@pytest.mark.asyncio
async def test_fetch_returns_none_when_unavailable(monkeypatch):
    monkeypatch.setattr(gallerydl, "find_gallerydl", lambda *_a, **_k: None)
    out = await gallerydl.fetch_tweets("a", "b", "TestHandle", settings={})
    assert out is None  # => TWClient falls back to GraphQL


@pytest.mark.asyncio
async def test_fetch_full_pipeline(monkeypatch):
    monkeypatch.setattr(gallerydl, "find_gallerydl", lambda *_a, **_k: "/usr/bin/gallery-dl")

    async def fake_run(exe, url, cookie_path, settings, range_spec=None):
        assert url == "https://x.com/TestHandle/tweets"
        return 0, SAMPLE, ""

    monkeypatch.setattr(gallerydl, "_run", fake_run)
    out = await gallerydl.fetch_tweets("a", "b", "@TestHandle", settings={})
    assert out is not None and len(out) == 2
    assert out[0]["views"] == 100


@pytest.mark.asyncio
async def test_fetch_returns_none_on_nonzero_exit(monkeypatch):
    monkeypatch.setattr(gallerydl, "find_gallerydl", lambda *_a, **_k: "/usr/bin/gallery-dl")

    async def fake_run(*_a, **_k):
        return 1, "", "HTTPError 401 AuthRequired"

    monkeypatch.setattr(gallerydl, "_run", fake_run)
    out = await gallerydl.fetch_tweets("a", "b", "TestHandle", settings={})
    assert out is None  # nonzero exit => fall back, not a silent empty success


@pytest.mark.asyncio
async def test_validate_true_false_none(monkeypatch):
    monkeypatch.setattr(gallerydl, "find_gallerydl", lambda *_a, **_k: "/usr/bin/gallery-dl")

    async def ok(*_a, **_k):
        return 0, "[]", ""

    async def auth_fail(*_a, **_k):
        return 1, "", "AuthRequired: authenticated cookies needed"

    async def ambiguous(*_a, **_k):
        return 1, "", "ConnectionError: network unreachable"

    monkeypatch.setattr(gallerydl, "_run", ok)
    assert await gallerydl.validate("a", "b", "h", settings={}) is True

    monkeypatch.setattr(gallerydl, "_run", auth_fail)
    assert await gallerydl.validate("a", "b", "h", settings={}) is False

    monkeypatch.setattr(gallerydl, "_run", ambiguous)
    assert await gallerydl.validate("a", "b", "h", settings={}) is None  # => GraphQL fallback


@pytest.mark.asyncio
async def test_validate_none_when_unavailable(monkeypatch):
    monkeypatch.setattr(gallerydl, "find_gallerydl", lambda *_a, **_k: None)
    assert await gallerydl.validate("a", "b", "h", settings={}) is None


# ── Backend ORDER in TWClient.get_all_tweets ─────────────────────────────────
# The user's requirement: gallery-dl is the primary (free) path; the paid
# official API is the FALLBACK, reached only when gallery-dl returns None.

@pytest.mark.asyncio
async def test_get_all_tweets_prefers_gallerydl_over_official(monkeypatch):
    from clients.tw import client as tw_client, official_api

    called = {"gdl": False, "official": False}

    async def gdl_ok(auth, ct0, user, settings=None):
        called["gdl"] = True
        return [{"tweet_id": "1", "likes": 7}]

    async def official_should_not_run(*_a, **_k):
        called["official"] = True
        return [{"tweet_id": "99", "likes": 0}]

    monkeypatch.setattr(gallerydl, "fetch_tweets", gdl_ok)
    monkeypatch.setattr(official_api, "fetch_tweets", official_should_not_run)

    c = tw_client.TWClient("at", "ct0", "handle")
    try:
        result = await c.get_all_tweets()
    finally:
        await c.close()

    assert result == [{"tweet_id": "1", "likes": 7}]   # gallery-dl's result
    assert called["gdl"] is True
    assert called["official"] is False                 # paid API never touched


@pytest.mark.asyncio
async def test_get_all_tweets_falls_back_to_official_when_gallerydl_none(monkeypatch):
    from clients.tw import client as tw_client, official_api

    called = {"gdl": False, "official": False}

    async def gdl_fails(auth, ct0, user, settings=None):
        called["gdl"] = True
        return None                                    # gallery-dl unavailable/failed

    async def official_rescues(*_a, **_k):
        called["official"] = True
        return [{"tweet_id": "42", "likes": 3}]

    monkeypatch.setattr(gallerydl, "fetch_tweets", gdl_fails)
    monkeypatch.setattr(official_api, "fetch_tweets", official_rescues)

    c = tw_client.TWClient("at", "ct0", "handle")
    try:
        result = await c.get_all_tweets()
    finally:
        await c.close()

    assert result == [{"tweet_id": "42", "likes": 3}]  # official API's result
    assert called["gdl"] is True and called["official"] is True


# ── Follower count rides the free timeline scrape ───────────────────────────
# gallery-dl's tweet metadata already carries author.followers_count, so a poll
# that scraped tweets for free must NOT then spend a billed official-API call
# just to snapshot the follower total.

def test_extract_follower_count_prefers_matching_handle():
    # _MEDIA_KW's author is TestHandle w/ 42 followers; the text tweet omits it.
    assert gallerydl._extract_follower_count(SAMPLE, "TestHandle") == 42
    assert gallerydl._extract_follower_count(SAMPLE, "@TestHandle") == 42


def test_extract_follower_count_falls_back_to_first_author():
    # No author matches the requested handle → take the first reported count.
    raw = json.dumps([[3, "https://pbs.twimg.com/media/AAA.jpg", dict(_MEDIA_KW)]])
    assert gallerydl._extract_follower_count(raw, "SomeoneElse") == 42


def test_extract_follower_count_none_when_absent():
    # A dump whose only tweet omits followers_count → None (nothing to cache).
    raw = json.dumps([[2, dict(_TEXT_KW)]])
    assert gallerydl._extract_follower_count(raw, "TestHandle") is None
    assert gallerydl._extract_follower_count("not json", "h") is None
    assert gallerydl._extract_follower_count("[]", "h") is None


@pytest.mark.asyncio
async def test_fetch_warms_follower_cache(monkeypatch):
    gallerydl._LAST_FOLLOWERS.pop("testhandle", None)
    monkeypatch.setattr(gallerydl, "find_gallerydl", lambda *_a, **_k: "/usr/bin/gallery-dl")

    async def fake_run(*_a, **_k):
        return 0, SAMPLE, ""

    monkeypatch.setattr(gallerydl, "_run", fake_run)
    await gallerydl.fetch_tweets("a", "b", "@TestHandle", settings={})
    # The follower count is now available for free — no network call.
    assert gallerydl.get_follower_count("TestHandle", settings={}) == 42


@pytest.mark.asyncio
async def test_failed_fetch_invalidates_stale_follower_cache(monkeypatch):
    # A previous cycle cached 99; this cycle's gallery-dl fetch fails.
    gallerydl._LAST_FOLLOWERS["testhandle"] = 99
    monkeypatch.setattr(gallerydl, "find_gallerydl", lambda *_a, **_k: "/usr/bin/gallery-dl")

    async def fail_run(*_a, **_k):
        return 1, "", "HTTPError 401 AuthRequired"

    monkeypatch.setattr(gallerydl, "_run", fail_run)
    out = await gallerydl.fetch_tweets("a", "b", "TestHandle", settings={})
    assert out is None
    # Stale 99 must be gone so the caller falls through to the paid/scrape path.
    assert gallerydl.get_follower_count("TestHandle", settings={}) is None


def test_get_follower_count_none_when_backend_off():
    gallerydl._LAST_FOLLOWERS["testhandle"] = 55
    try:
        # "official"/"graphql" force gallery-dl off — its stale cache must not leak.
        assert gallerydl.get_follower_count("TestHandle", {"tw_polling_backend": "official"}) is None
        assert gallerydl.get_follower_count("TestHandle", {"tw_polling_backend": "graphql"}) is None
    finally:
        gallerydl._LAST_FOLLOWERS.pop("testhandle", None)


@pytest.mark.asyncio
async def test_client_follower_count_prefers_gallerydl(monkeypatch):
    from clients.tw import client as tw_client, official_api

    called = {"official": False}
    monkeypatch.setattr(gallerydl, "get_follower_count", lambda *_a, **_k: 123)

    async def official_should_not_run(*_a, **_k):
        called["official"] = True
        return 999

    monkeypatch.setattr(official_api, "get_follower_count", official_should_not_run)

    c = tw_client.TWClient("at", "ct0", "handle")
    try:
        fc = await c.get_follower_count()
    finally:
        await c.close()

    assert fc == 123                 # gallery-dl's free cached count
    assert called["official"] is False   # paid API never touched


@pytest.mark.asyncio
async def test_client_follower_count_falls_back_to_official(monkeypatch):
    from clients.tw import client as tw_client, official_api

    monkeypatch.setattr(gallerydl, "get_follower_count", lambda *_a, **_k: None)

    async def official_rescues(*_a, **_k):
        return 77

    monkeypatch.setattr(official_api, "get_follower_count", official_rescues)

    c = tw_client.TWClient("at", "ct0", "handle")
    try:
        fc = await c.get_follower_count()
    finally:
        await c.close()

    assert fc == 77                  # official API's value when gallery-dl has none
