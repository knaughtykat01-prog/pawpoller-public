"""X/Twitter poller skips reposts (retweets of other accounts).

The UserTweets timeline interleaves the account's own posts/replies with its
retweets. A retweet's engagement belongs to the original author, so the poller
must skip reposts while keeping the account's own posts, replies, and quotes.
"""

from clients.tw.client import (
    _is_repost, _is_own_repost, _repost_original, _snowflake_to_utc, TWClient,
)


def _result(legacy: dict) -> dict:
    return {"rest_id": "123", "legacy": legacy}


def _repost(original_legacy: dict) -> dict:
    return {"rest_id": "1", "legacy": {
        "retweeted_status_result": {"result": {"rest_id": "99", "legacy": original_legacy}}}}


def test_repost_is_skipped():
    assert _is_repost(_result({"retweeted_status_result": {"result": {"rest_id": "999"}}})) is True


def test_original_tweet_is_kept():
    assert _is_repost(_result({"full_text": "hello world", "favorite_count": 3})) is False


def test_reply_is_kept():
    # A reply (comment) by the account is its own content — keep it.
    assert _is_repost(_result({"in_reply_to_status_id_str": "555", "full_text": "agreed"})) is False


def test_quote_tweet_is_kept():
    # A quote is the account's own post quoting another — not a repost.
    assert _is_repost(_result({"quoted_status_id_str": "777", "full_text": "look at this"})) is False


def test_missing_or_empty_is_not_repost():
    assert _is_repost({}) is False
    assert _is_repost({"legacy": {}}) is False
    assert _is_repost(None) is False


# ── Only reposts of the account's OWN posts are kept ──

def _repost_by(author_user: dict) -> dict:
    r = _repost({"full_text": "hi", "favorite_count": 9, "entities": {}})
    r["legacy"]["retweeted_status_result"]["result"]["core"] = {"user_results": {"result": author_user}}
    return r


def test_self_repost_is_own():
    legacy_shape = _repost_by({"legacy": {"screen_name": "TestHandle"}})
    core_shape = _repost_by({"core": {"screen_name": "TestHandle"}})   # X's newer layout
    assert _is_own_repost(legacy_shape, "@testhandle") is True          # @ + case-insensitive
    assert _is_own_repost(core_shape, "TestHandle") is True


def test_repost_of_someone_else_is_dropped_even_if_tagged():
    r = _repost({"entities": {"user_mentions": [{"screen_name": "TestHandle"}]},
                 "full_text": "look @TestHandle I drew you"})
    r["legacy"]["retweeted_status_result"]["result"]["core"] = {
        "user_results": {"result": {"legacy": {"screen_name": "artist"}}}}
    assert _is_repost(r) and _is_own_repost(r, "TestHandle") is False


def test_empty_target_or_missing_author_never_own():
    assert _is_own_repost(_repost_by({"legacy": {"screen_name": "x"}}), "") is False
    assert _is_own_repost(_repost({"full_text": "hi"}), "TestHandle") is False


def test_repost_original_is_extracted():
    orig = _repost_original(_repost({"full_text": "hi", "entities": {}}))
    assert orig and orig.get("rest_id") == "99"


def test_timeline_walk_keeps_self_repost_as_the_own_post_and_quote_own_stats():
    import asyncio
    own = {"rest_id": "99", "legacy": {"full_text": "my art", "favorite_count": 9, "entities": {}},
           "core": {"user_results": {"result": {"legacy": {"screen_name": "TestHandle"}}}}}
    self_rt = {"rest_id": "1", "legacy": {"retweeted_status_result": {"result": own}}}
    other_rt = {"rest_id": "2", "legacy": {"retweeted_status_result": {"result": {
        "rest_id": "50", "legacy": {"full_text": "hi @TestHandle", "favorite_count": 500,
                                    "entities": {"user_mentions": [{"screen_name": "TestHandle"}]}},
        "core": {"user_results": {"result": {"legacy": {"screen_name": "artist"}}}}}}}}
    quote = {"rest_id": "3", "legacy": {"full_text": "look", "favorite_count": 4,
                                        "quoted_status_id_str": "50", "entities": {}},
             "quoted_status_result": {"result": {"rest_id": "50", "legacy": {"favorite_count": 500}}}}
    entry = lambda i, r: {"entryId": f"tweet-{i}", "content": {"itemContent": {"tweet_results": {"result": r}}}}
    page = {"data": {"user": {"result": {"timeline_v2": {"timeline": {"instructions": [
        {"type": "TimelineAddEntries", "entries": [entry(1, self_rt), entry(2, other_rt),
                                                   entry(3, quote), entry(4, own)]}]}}}}}}

    c = TWClient("a", "b", "TestHandle")
    c._user_rest_id = "42"
    async def fake_get_json(url, params=None):
        return page
    c._get_json = fake_get_json
    got = {d["tweet_id"]: d for d in asyncio.run(c._get_all_tweets_graphql())}
    assert set(got) == {"99", "3"}                       # self-RT merged with its post; other RT gone
    assert got["99"]["content_type"] == "tweet" and got["99"]["likes"] == 9
    assert got["3"]["content_type"] == "quote" and got["3"]["likes"] == 4   # the quote's own stats


# ── Core fix: stats parsed straight from the timeline result ──

def test_extract_stats_from_timeline_result():
    c = TWClient("a", "b", "TestHandle")
    result = {
        "rest_id": "123",
        "legacy": {"full_text": "hello world", "favorite_count": 5, "retweet_count": 2,
                   "reply_count": 1, "quote_count": 0, "created_at": "x", "entities": {}},
        "views": {"count": "100"},
        "core": {"user_results": {"result": {"legacy": {"screen_name": "TestHandle"}}}},
    }
    d = c._extract_tweet_stats(result)
    assert d["tweet_id"] == "123"
    assert d["likes"] == 5 and d["retweets"] == 2 and d["views"] == 100
    assert d["title"].startswith("hello world")
    assert d["link"] == "https://x.com/TestHandle/status/123"


# ── Dates derived from the Snowflake id (X stopped filling created_at) ──

def test_snowflake_date_extraction():
    # A real tweet id → its encoded creation time (UTC). 1700000000000000000
    # decodes to early September 2023.
    out = _snowflake_to_utc("1700000000000000000")
    assert out.startswith("2023-09")


def test_snowflake_date_format_is_parseable():
    out = _snowflake_to_utc(1445919810076827648)
    # "YYYY-MM-DD HH:MM:SS" shape that Utils._parseDate handles.
    assert len(out) == 19 and out[4] == "-" and out[13] == ":"


def test_snowflake_bad_id_returns_empty():
    assert _snowflake_to_utc("") == ""
    assert _snowflake_to_utc("not-a-number") == ""
    assert _snowflake_to_utc(None) == ""


def test_quote_tweet_grabs_quoted_post_image():
    # A quote tweet has no media of its own; the image is in the quoted post.
    c = TWClient("a", "b", "TestHandle")
    result = {
        "rest_id": "1994383496316678229",
        "legacy": {"full_text": "And the second accompanying piece by @Duskhare_owo",
                   "favorite_count": 0, "quoted_status_id_str": "555", "entities": {}},
        "quoted_status_result": {"result": {
            "rest_id": "555",
            "legacy": {"full_text": "art!", "extended_entities": {"media": [
                {"media_url_https": "https://pbs.twimg.com/media/QUOTED.jpg"}]}},
        }},
    }
    d = c._extract_tweet_stats(result)
    assert d["content_type"] == "quote"
    assert d["thumbnail_url"] == "https://pbs.twimg.com/media/QUOTED.jpg"


def test_extract_uses_snowflake_when_created_at_missing():
    c = TWClient("a", "b", "TestHandle")
    result = {"rest_id": "1445919810076827648",
              "legacy": {"full_text": "hi", "favorite_count": 0, "entities": {}}}
    d = c._extract_tweet_stats(result)
    assert d["posted_at"]  # non-empty, derived from the id
    assert d["posted_at"][:4].isdigit()
