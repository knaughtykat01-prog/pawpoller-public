"""Unit tests for the Mastodon (mast) client's parsing + filtering logic.

These exercise the pure helpers and _parse_status against fixture status
objects — no network. They lock in the favourites→likes / reblogs→reposts
mapping, content-type detection, HTML stripping, instance normalisation, and
the "keep a boost only when the account is @-tagged" rule.
"""

import asyncio

import pytest

from clients.mast.client import (
    MastClient, _strip_html, _normalise_instance,
    _status_mentions_account, _safe_int, detect_flavour,
)


@pytest.mark.parametrize("version,title,source_url,expected", [
    # Real `/api/v1/instance` version strings observed live (2026-07-31).
    ("4.7.0-alpha.2+pr-39999-68e5caf", "Mastodon", "", "Mastodon"),
    ("2.7.2 (compatible; Pleroma 2.10.2)", "", "", "Pleroma"),
    ("2.7.2 (compatible; Akkoma 3.19.0-0-g4ceb81a--stable-)", "", "", "Akkoma"),
    ("3.5.3 (compatible; Pixelfed 0.12.7)", "pixelfed", "", "Pixelfed"),
    # GoToSocial doesn't use the compatible tail — fall back to source/title.
    ("0.22.1+git-fdff42b", "SuperSeriousBusiness GoToSocial",
     "https://github.com/superseriousbusiness/gotosocial", "GoToSocial"),
    ("0.22.1", "", "https://github.com/superseriousbusiness/gotosocial", "GoToSocial"),
    # Unknown / bare semver → assume Mastodon (the connector still works).
    ("", "", "", "Mastodon"),
])
def test_detect_flavour(version, title, source_url, expected):
    assert detect_flavour(version, title, source_url) == expected


def _status(**over):
    """A minimal Mastodon status object with sensible defaults."""
    base = {
        "id": "111",
        "uri": "https://m.example/users/me/statuses/111",
        "url": "https://m.example/@me/111",
        "content": "<p>Hello <b>world</b></p>",
        "created_at": "2026-06-30T01:02:03.000Z",
        "favourites_count": 12,
        "reblogs_count": 3,
        "replies_count": 1,
        "in_reply_to_id": None,
        "reblog": None,
        "media_attachments": [],
        "account": {"id": "1", "acct": "me", "username": "me"},
        "mentions": [],
        "tags": [],
        "sensitive": False,
    }
    base.update(over)
    return base


def test_strip_html():
    assert _strip_html("<p>Hello <b>world</b></p><p>line two</p>") == "Hello world line two"
    assert _strip_html("a&amp;b") == "a&b"
    assert _strip_html("") == ""


def test_normalise_instance():
    assert _normalise_instance("mastodon.social") == "https://mastodon.social"
    assert _normalise_instance("https://pawb.fun/") == "https://pawb.fun"
    assert _normalise_instance("  http://x.tld/  ") == "http://x.tld"
    assert _normalise_instance("") == ""


def test_safe_int():
    assert _safe_int(None) == 0
    assert _safe_int("1,234") == 1234
    assert _safe_int(7) == 7


def test_parse_status_maps_counts_and_type():
    c = MastClient(instance_url="https://m.example", access_token="t")
    d = c._parse_status(_status())
    assert d["likes"] == 12 and d["reposts"] == 3 and d["replies"] == 1
    assert d["quotes"] == 0           # Mastodon has no native quote count
    assert d["content_type"] == "post"
    assert d["full_text"] == "Hello world"
    assert d["link"] == "https://m.example/@me/111"
    assert d["post_uri"] == "https://m.example/users/me/statuses/111"


def test_parse_status_reply_and_media():
    c = MastClient(instance_url="https://m.example", access_token="t")
    d = c._parse_status(_status(
        in_reply_to_id="999",
        media_attachments=[{"type": "image", "preview_url": "https://m.example/thumb.png"}],
        sensitive=True,
    ))
    assert d["content_type"] == "reply"
    assert d["has_media"] == 1
    assert d["thumbnail_url"] == "https://m.example/thumb.png"
    assert d["rating"] == "Mature"


def test_status_mentions_account():
    s = _status(mentions=[{"id": "42", "acct": "me"}])
    assert _status_mentions_account(s, "42") is True
    assert _status_mentions_account(s, "7") is False
    assert _status_mentions_account(s, "") is False


def test_get_all_post_uris_drops_untagged_reblog_keeps_tagged():
    """A boost is dropped unless the account is @-mentioned in the boosted post;
    when kept, it's flagged content_type='repost' and tracks the ORIGINAL."""
    c = MastClient(instance_url="https://m.example", access_token="t")
    c._account_id = "1"
    c._logged_in = True

    own = _status(id="10", uri="uri-own")
    tagged_boost = _status(id="20", reblog=_status(
        id="21", uri="uri-orig-tagged", mentions=[{"id": "1", "acct": "me"}]))
    untagged_boost = _status(id="30", reblog=_status(id="31", uri="uri-orig-untagged"))

    pages = [[own, tagged_boost, untagged_boost], []]

    async def fake_get_json(path, params=None):
        return pages.pop(0) if pages else []

    c._get_json = fake_get_json

    async def _run():
        items = await c.get_all_post_uris()
        details = await c.get_post_details_batch(items)
        return items, details

    items, details = asyncio.run(_run())
    uris = {i["post_uri"] for i in items}
    assert uris == {"uri-own", "uri-orig-tagged"}     # untagged boost dropped
    repost = next(i for i in items if i["post_uri"] == "uri-orig-tagged")
    assert repost.get("content_type") == "repost"

    by_uri = {d["post_uri"]: d for d in details}
    assert by_uri["uri-orig-tagged"]["content_type"] == "repost"
    assert by_uri["uri-own"]["content_type"] == "post"
