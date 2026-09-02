"""Bluesky poller skips reposts (the actor reposting someone else's post).

getAuthorFeed interleaves the actor's own posts/replies with reposts. A repost
item's `post` is the original author's, so its stats aren't the actor's and must
be skipped. Pinned posts (reasonPin) are the actor's own and are kept.
"""

from clients.bsky.client import _is_repost_item, _post_mentions_did, BskyClient


def test_repost_item_is_skipped():
    item = {
        "post": {"uri": "at://did:plc:other/app.bsky.feed.post/abc"},
        "reason": {"$type": "app.bsky.feed.defs#reasonRepost"},
    }
    assert _is_repost_item(item) is True


def test_own_post_is_kept():
    assert _is_repost_item({"post": {"uri": "at://did:plc:me/app.bsky.feed.post/abc"}}) is False


def test_reply_is_kept():
    # A reply item (no reason) is the actor's own comment — keep it.
    item = {"post": {"uri": "at://did:plc:me/app.bsky.feed.post/r1"}, "reply": {"parent": {}}}
    assert _is_repost_item(item) is False


def test_pinned_post_is_kept():
    item = {
        "post": {"uri": "at://did:plc:me/app.bsky.feed.post/pinned"},
        "reason": {"$type": "app.bsky.feed.defs#reasonPin"},
    }
    assert _is_repost_item(item) is False


def test_missing_or_malformed_reason():
    assert _is_repost_item({}) is False
    assert _is_repost_item({"reason": None}) is False
    assert _is_repost_item({"reason": "weird"}) is False


# ── Tagged reposts + content-type detection (Post/Reply/Quote/Repost) ──

def test_post_mentions_did():
    post = {"record": {"facets": [
        {"features": [{"$type": "app.bsky.richtext.facet#mention", "did": "did:plc:me"}]}]}}
    assert _post_mentions_did(post, "did:plc:me") is True
    assert _post_mentions_did(post, "did:plc:other") is False
    assert _post_mentions_did(post, "") is False
    assert _post_mentions_did({}, "did:plc:me") is False


def _bsky_client():
    c = BskyClient("me.bsky.social", "pw")
    c._handle = "me.bsky.social"
    return c


def test_parse_post_plain_is_post():
    p = {"uri": "at://x/app.bsky.feed.post/1", "record": {"text": "hi"}, "author": {"handle": "me"}}
    assert _bsky_client()._parse_post(p)["content_type"] == "post"


def test_parse_post_reply():
    p = {"uri": "at://x/app.bsky.feed.post/2",
         "record": {"text": "agreed", "reply": {"parent": {}, "root": {}}}, "author": {"handle": "me"}}
    assert _bsky_client()._parse_post(p)["content_type"] == "reply"


def test_parse_post_quote():
    p = {"uri": "at://x/app.bsky.feed.post/3", "record": {"text": "nice"},
         "author": {"handle": "me"}, "embed": {"$type": "app.bsky.embed.record#view"}}
    assert _bsky_client()._parse_post(p)["content_type"] == "quote"


def test_parse_post_quote_with_media_thumbnail():
    p = {"uri": "at://x/app.bsky.feed.post/4", "record": {"text": "art"}, "author": {"handle": "me"},
         "embed": {"$type": "app.bsky.embed.recordWithMedia#view",
                   "media": {"$type": "app.bsky.embed.images#view",
                             "images": [{"thumb": "https://cdn.bsky.app/x.jpg"}]}}}
    d = _bsky_client()._parse_post(p)
    assert d["content_type"] == "quote"
    assert d["thumbnail_url"] == "https://cdn.bsky.app/x.jpg"
