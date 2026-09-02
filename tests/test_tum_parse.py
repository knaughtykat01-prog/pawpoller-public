"""Unit tests for the Tumblr (tum) client's parsing helpers — no network.

Locks in note_count → notes mapping, post-type passthrough, HTML-stripped
titles, photo thumbnails, timestamp formatting, and blog-identifier
normalisation.
"""

import asyncio

from clients.tum.client import TumClient, _strip_html, _normalise_blog, _safe_int


def _post(**over):
    base = {
        "id_string": "12345",
        "post_url": "https://staff.tumblr.com/post/12345",
        "type": "text",
        "timestamp": 1751250000,   # 2025-06-30-ish UTC
        "title": "Hello world",
        "summary": "Hello world summary",
        "body": "<p>Hello <b>world</b></p>",
        "note_count": 4321,
        "tags": ["foo", "bar"],
        "blog_name": "staff",
        "photos": [],
    }
    base.update(over)
    return base


def test_strip_html():
    assert _strip_html("<p>Hi <i>there</i></p><p>two</p>") == "Hi there two"
    assert _strip_html("a&amp;b") == "a&b"


def test_normalise_blog():
    assert _normalise_blog("@Staff.tumblr.com/") == "Staff.tumblr.com"
    assert _normalise_blog("https://staff.tumblr.com/") == "staff.tumblr.com"
    assert _normalise_blog("  staff  ") == "staff"
    assert _normalise_blog("") == ""


def test_safe_int():
    assert _safe_int("4,321") == 4321
    assert _safe_int(None) == 0


def test_parse_post_maps_notes_and_type():
    c = TumClient(api_key="k", blog="staff")
    d = c._parse_post(_post())
    assert d["notes"] == 4321
    assert d["content_type"] == "text"
    assert d["post_uri"] == "12345"
    assert d["title"] == "Hello world"
    assert d["link"] == "https://staff.tumblr.com/post/12345"
    assert d["posted_at"].startswith("2025-")   # formatted from unix timestamp


def test_parse_post_photo_thumbnail():
    c = TumClient(api_key="k", blog="staff")
    d = c._parse_post(_post(
        type="photo", title=None, summary=None,
        photos=[{"original_size": {"url": "https://64.media.tumblr.com/x.jpg"}}],
    ))
    assert d["content_type"] == "photo"
    assert d["thumbnail_url"] == "https://64.media.tumblr.com/x.jpg"
    assert d["has_media"] == 1


def test_get_all_post_uris_paginates_and_dedupes():
    c = TumClient(api_key="k", blog="staff")
    c._blog_name = "staff"
    c._logged_in = True

    page1 = {"response": {"posts": [_post(id_string=str(i)) for i in range(20)]}}
    page2 = {"response": {"posts": [_post(id_string=str(i)) for i in range(20, 31)]}}
    pages = [page1, page2]

    async def fake_get_json(path, params=None):
        return pages.pop(0) if pages else {"response": {"posts": []}}

    c._get_json = fake_get_json

    async def _run():
        items = await c.get_all_post_uris()
        details = await c.get_post_details_batch(items)
        return items, details

    items, details = asyncio.run(_run())
    assert len(items) == 31                     # 20 + 11, paginated
    assert len({i["post_uri"] for i in items}) == 31  # all unique
    assert all("notes" in d for d in details)
