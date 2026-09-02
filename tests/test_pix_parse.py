"""Unit tests for the Pixiv (pix) client parsing + OAuth-hash helpers — no network.

Locks in total_view/total_bookmarks/total_comments → views/favorites/comments,
namespaced ids, illust-vs-novel typing/links, x_restrict → rating, and the
X-Client-Hash construction used for the token refresh.
"""

import asyncio
import hashlib

from clients.pix.client import PixClient, _safe_int, _HASH_SECRET


def _illust(**over):
    base = {
        "id": 12345,
        "title": "My Art",
        "type": "illust",
        "create_date": "2026-06-30T01:02:03+09:00",
        "total_view": 10000,
        "total_bookmarks": 850,
        "total_comments": 12,
        "x_restrict": 0,
        "image_urls": {"medium": "https://i.pximg.net/img/medium/12345.jpg"},
        "tags": [{"name": "foo"}, {"name": "bar"}],
        "caption": "<p>hi</p>",
        "user": {"name": "Artist", "account": "artist"},
    }
    base.update(over)
    return base


def test_safe_int():
    assert _safe_int("1,234") == 1234
    assert _safe_int(None) == 0


def test_parse_illust_maps_metrics():
    c = PixClient(refresh_token="r")
    d = c._parse_work(_illust(), "illust")
    assert d["views"] == 10000
    assert d["favorites_count"] == 850
    assert d["comments_count"] == 12
    assert d["post_uri"] == "illust:12345"
    assert d["content_type"] == "illust"
    assert d["link"] == "https://www.pixiv.net/artworks/12345"
    assert d["thumbnail_url"] == "https://i.pximg.net/img/medium/12345.jpg"
    assert d["rating"] == "General"


def test_parse_novel_and_rating():
    c = PixClient(refresh_token="r")
    d = c._parse_work(_illust(id=999, x_restrict=1), "novel")
    assert d["post_uri"] == "novel:999"
    assert d["content_type"] == "novel"
    assert d["link"] == "https://www.pixiv.net/novel/show.php?id=999"
    assert d["rating"] == "R-18"


def test_client_hash_construction():
    # The token refresh signs X-Client-Time with md5(time + secret); lock the
    # algorithm so a future refactor can't silently break auth.
    t = "2026-06-30T00:00:00+00:00"
    expected = hashlib.md5((t + _HASH_SECRET).encode("utf-8")).hexdigest()
    assert len(expected) == 32 and all(ch in "0123456789abcdef" for ch in expected)


def test_get_all_post_uris_combines_illusts_and_novels():
    c = PixClient(refresh_token="r", user_id="42")
    c._logged_in = True
    c._access_token = "tok"

    illust_page = {"illusts": [_illust(id=1), _illust(id=2)], "next_url": None}
    novel_page = {"novels": [_illust(id=3)], "next_url": None}

    async def fake_get_json(url, params=None):
        return illust_page if "illusts" in url else novel_page

    c._get_json = fake_get_json

    async def _run():
        items = await c.get_all_post_uris()
        details = await c.get_post_details_batch(items)
        return items, details

    items, details = asyncio.run(_run())
    uris = {i["post_uri"] for i in items}
    assert uris == {"illust:1", "illust:2", "novel:3"}
    by_uri = {d["post_uri"]: d for d in details}
    assert by_uri["novel:3"]["content_type"] == "novel"
    assert by_uri["illust:1"]["content_type"] == "illust"
