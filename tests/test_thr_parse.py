"""Unit tests for the Threads (thr) client parsing + insights helpers — no network.

Locks in the insights → views/likes/reposts/replies/quotes mapping (both the
total_value and values[] response shapes), media_type → content_type, quote
detection, and the discovery→details flow.
"""

import asyncio

from clients.thr.client import ThrClient, _safe_int, _MEDIA_TYPE_MAP


def _post(**over):
    base = {
        "id": "17900000000000000",
        "media_type": "IMAGE",
        "text": "Hello Threads",
        "permalink": "https://www.threads.net/@me/post/abc",
        "timestamp": "2026-06-30T01:02:03+0000",
        "is_quote_post": False,
        "thumbnail_url": "https://scontent.cdninstagram.com/x.jpg",
        "username": "me",
    }
    base.update(over)
    return base


def test_safe_int():
    assert _safe_int("1,234") == 1234
    assert _safe_int(None) == 0


def test_parse_post_types():
    c = ThrClient(access_token="t")
    assert c._parse_post(_post(media_type="TEXT_POST"), {})["content_type"] == "text"
    assert c._parse_post(_post(media_type="VIDEO"), {})["content_type"] == "video"
    assert c._parse_post(_post(media_type="CAROUSEL_ALBUM"), {})["content_type"] == "carousel"
    assert c._parse_post(_post(is_quote_post=True), {})["content_type"] == "quote"


def test_parse_post_merges_insights():
    c = ThrClient(access_token="t")
    insights = {"views": 5000, "likes": 120, "reposts": 8, "replies": 15, "quotes": 3}
    d = c._parse_post(_post(), insights)
    assert d["views"] == 5000 and d["likes"] == 120 and d["reposts"] == 8
    assert d["replies"] == 15 and d["quotes"] == 3
    assert d["post_uri"] == "17900000000000000"
    assert d["link"] == "https://www.threads.net/@me/post/abc"
    assert d["has_media"] == 1


def test_get_insights_handles_both_shapes():
    """Threads insights come back as either total_value or values[]."""
    c = ThrClient(access_token="t")

    async def fake_get_json(url, params=None):
        return {"data": [
            {"name": "views", "total_value": {"value": 999}},      # newer shape
            {"name": "likes", "values": [{"value": 42}]},          # older shape
            {"name": "replies", "total_value": {"value": 7}},
        ]}

    c._get_json = fake_get_json
    out = asyncio.run(c._get_insights("123"))
    assert out["views"] == 999
    assert out["likes"] == 42
    assert out["replies"] == 7
    assert out["reposts"] == 0   # absent metric defaults to 0


def test_get_all_post_uris_then_details():
    c = ThrClient(access_token="t", user_id="42")
    c._logged_in = True

    listing = {"data": [_post(id="1"), _post(id="2")], "paging": {}}
    insights = {"data": [{"name": "views", "total_value": {"value": 10}},
                         {"name": "likes", "total_value": {"value": 2}}]}

    async def fake_get_json(url, params=None):
        return insights if "/insights" in url else listing

    c._get_json = fake_get_json

    async def _run():
        items = await c.get_all_post_uris()
        details = await c.get_post_details_batch(items)
        return items, details

    items, details = asyncio.run(_run())
    assert {i["post_uri"] for i in items} == {"1", "2"}
    assert all(d["views"] == 10 and d["likes"] == 2 for d in details)
