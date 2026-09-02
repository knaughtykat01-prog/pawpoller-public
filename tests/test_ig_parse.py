"""Unit tests for the Instagram (ig) client parsing + insights helpers — no network.

Locks in the split data model: likes/comments come off the media object
(like_count / comments_count) while views/reach/saved/shares come from the
per-media /insights call (both the total_value and values[] response shapes),
media_type → content_type (incl. media_product_type REELS → reel), and the
discovery→details flow.
"""

import asyncio

from clients.ig.client import IgClient, _safe_int, _MEDIA_TYPE_MAP


def _post(**over):
    base = {
        "id": "17900000000000000",
        "media_type": "IMAGE",
        "caption": "Hello Instagram",
        "permalink": "https://www.instagram.com/p/abc/",
        "timestamp": "2026-06-30T01:02:03+0000",
        "thumbnail_url": "https://scontent.cdninstagram.com/x.jpg",
        "like_count": 0,
        "comments_count": 0,
        "username": "me",
    }
    base.update(over)
    return base


def test_safe_int():
    assert _safe_int("1,234") == 1234
    assert _safe_int(None) == 0


def test_parse_post_types():
    c = IgClient(access_token="t")
    assert c._parse_post(_post(media_type="IMAGE"), {})["content_type"] == "image"
    assert c._parse_post(_post(media_type="VIDEO"), {})["content_type"] == "video"
    assert c._parse_post(_post(media_type="CAROUSEL_ALBUM"), {})["content_type"] == "carousel"
    # A reel reports media_type VIDEO but media_product_type REELS.
    assert c._parse_post(_post(media_type="VIDEO", media_product_type="REELS"), {})["content_type"] == "reel"


def test_parse_post_splits_media_object_and_insights():
    c = IgClient(access_token="t")
    # likes/comments come from the media object; the rest from insights.
    insights = {"views": 5000, "reach": 3000, "saved": 12, "shares": 4}
    d = c._parse_post(_post(like_count=120, comments_count=15), insights)
    assert d["views"] == 5000 and d["reach"] == 3000
    assert d["likes"] == 120 and d["comments"] == 15
    assert d["saved"] == 12 and d["shares"] == 4
    assert d["post_uri"] == "17900000000000000"
    assert d["link"] == "https://www.instagram.com/p/abc/"
    assert d["has_media"] == 1


def test_get_insights_handles_both_shapes():
    """Instagram media insights come back as either total_value or values[]."""
    c = IgClient(access_token="t")

    async def fake_get_json(url, params=None):
        return {"data": [
            {"name": "views", "total_value": {"value": 999}},      # newer shape
            {"name": "reach", "values": [{"value": 42}]},          # older shape
            {"name": "saved", "total_value": {"value": 7}},
        ]}

    c._get_json = fake_get_json
    out = asyncio.run(c._get_insights("123"))
    assert out["views"] == 999
    assert out["reach"] == 42
    assert out["saved"] == 7
    assert out["shares"] == 0   # absent metric defaults to 0


def test_get_all_post_uris_then_details():
    c = IgClient(access_token="t", user_id="42")
    c._logged_in = True

    listing = {"data": [_post(id="1", like_count=2), _post(id="2", like_count=2)], "paging": {}}
    insights = {"data": [{"name": "views", "total_value": {"value": 10}},
                         {"name": "reach", "total_value": {"value": 8}}]}

    async def fake_get_json(url, params=None):
        return insights if "/insights" in url else listing

    c._get_json = fake_get_json

    async def _run():
        items = await c.get_all_post_uris()
        details = await c.get_post_details_batch(items)
        return items, details

    items, details = asyncio.run(_run())
    assert {i["post_uri"] for i in items} == {"1", "2"}
    # views/reach from insights, likes off the media object.
    assert all(d["views"] == 10 and d["reach"] == 8 and d["likes"] == 2 for d in details)
