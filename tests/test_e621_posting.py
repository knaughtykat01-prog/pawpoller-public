"""e621 posting + v2-polling tests.

Covers the two things added when e621 went poll+post:
  - _parse_post tolerates BOTH the legacy (flat file/score) and v2 extended
    (nested files/stats) response shapes.
  - E621Client.upload_post (POST /uploads.json) — success + rejection.
  - E621Poster validation, rating mapping, and the happy-path post.
"""

import pytest
import respx
import httpx

import config
from clients.e621.client import E621Client
from posting.platforms.e621 import E621Poster, _rating_to_e621
from posting.platforms.base import StoryUploadPackage


# ── Dual-shape parsing ───────────────────────────────────────

def test_parse_post_legacy_flat_shape():
    c = E621Client(username="tester", api_key="k")
    p = {
        "id": 111,
        "file": {"url": "https://x/f.png", "ext": "png"},
        "preview": {"url": "https://x/p.jpg"},
        "score": {"up": 60, "down": -10, "total": 50},
        "fav_count": 42, "comment_count": 3,
        "rating": "e", "description": "A test piece",
        "created_at": "2026-01-01T00:00:00",
        "tags": {"general": ["wolf", "male"], "artist": ["tester"]},
    }
    d = c._parse_post(p)
    assert d["post_uri"] == "111"
    assert d["score"] == 50 and d["up_score"] == 60 and d["down_score"] == -10
    assert d["favorites_count"] == 42 and d["comments_count"] == 3
    assert d["rating"] == "Explicit"
    assert d["file_url"] == "https://x/f.png"
    assert d["thumbnail_url"] == "https://x/p.jpg"
    assert d["keywords"] == ["wolf", "male", "tester"]


def test_parse_post_v2_extended_nested_shape():
    c = E621Client(username="tester", api_key="k")
    p = {
        "id": 222,
        "files": {
            "original": {"url": "https://x/o.png"},
            "meta": {"ext": "png"},
            "preview": {"jpg": "https://x/pv.jpg"},
            "sample": {"jpg": "https://x/sm.jpg"},
        },
        "stats": {"score": {"up": 30, "down": -2, "total": 28},
                  "fav_count": 17, "comment_count": 1},
        "rating": "q", "description": "",
        "created_at": "2026-02-02T00:00:00",
        "tags": {"general": ["fox"], "species": ["canine"]},
    }
    d = c._parse_post(p)
    assert d["post_uri"] == "222"
    assert d["score"] == 28 and d["up_score"] == 30 and d["down_score"] == -2
    assert d["favorites_count"] == 17 and d["comments_count"] == 1
    assert d["rating"] == "Questionable"
    assert d["file_url"] == "https://x/o.png"
    assert d["thumbnail_url"] == "https://x/pv.jpg"
    assert d["keywords"] == ["fox", "canine"]


# ── Rating map + validation ──────────────────────────────────

@pytest.mark.parametrize("rating,expected", [
    ("general", "s"), ("safe", "s"), ("sfw", "s"),
    ("mature", "q"), ("questionable", "q"),
    ("adult", "e"), ("explicit", "e"), ("nsfw", "e"),
    ("", "e"), ("garbage", "e"),
])
def test_rating_map(rating, expected):
    assert _rating_to_e621(rating) == expected


def _pkg(tags, file_path="/tmp/x.png", rating="adult", source=""):
    return StoryUploadPackage(
        story_name="Art", chapter_index=0, chapter_title="", platform="e621",
        title="Art", description="desc", tags=tags, rating=rating,
        file_path=file_path, extra={"source": source} if source else {},
    )


def test_validate_requires_tag_floor():
    p = E621Poster()
    errs = p.validate(_pkg(["wolf", "male"]))
    assert any("tag set" in e for e in errs)


def test_validate_requires_file():
    p = E621Poster()
    errs = p.validate(_pkg(["wolf", "male", "solo", "canine"], file_path=""))
    assert any("image file" in e for e in errs)


def test_validate_passes_with_enough_tags_and_file(upload_file):
    p = E621Poster()
    assert p.validate(_pkg(["wolf", "male", "solo", "canine"], file_path=upload_file)) == []


# ── Client upload ────────────────────────────────────────────

@pytest.mark.asyncio
@respx.mock
async def test_upload_post_success(tmp_path):
    img = tmp_path / "art.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
    respx.post("https://e621.net/uploads.json").mock(
        return_value=httpx.Response(200, json={
            "success": True, "location": "/posts/98765", "post_id": 98765}))
    c = E621Client(username="u", api_key="k")
    res = await c.upload_post(tag_string="wolf male solo canine", rating="e",
                              file_path=str(img), source="https://src/art")
    await c.close()
    assert res["success"] is True
    assert res["post_id"] == "98765"
    assert res["url"] == "https://e621.net/posts/98765"


@pytest.mark.asyncio
@respx.mock
async def test_upload_post_duplicate_rejected(tmp_path):
    img = tmp_path / "art.png"
    img.write_bytes(b"dupe")
    respx.post("https://e621.net/uploads.json").mock(
        return_value=httpx.Response(412, json={
            "success": False, "reason": "duplicate", "location": "/posts/555"}))
    c = E621Client(username="u", api_key="k")
    with pytest.raises(RuntimeError) as ei:
        await c.upload_post(tag_string="wolf male solo canine", rating="e",
                            file_path=str(img))
    await c.close()
    assert "duplicate" in str(ei.value)
    assert "/posts/555" in str(ei.value)


@pytest.mark.asyncio
async def test_upload_post_rejects_bad_rating(tmp_path):
    img = tmp_path / "art.png"
    img.write_bytes(b"x")
    c = E621Client(username="u", api_key="k")
    with pytest.raises(RuntimeError):
        await c.upload_post(tag_string="wolf", rating="banana", file_path=str(img))
    await c.close()


@pytest.mark.asyncio
async def test_upload_post_requires_exactly_one_source(tmp_path):
    c = E621Client(username="u", api_key="k")
    with pytest.raises(RuntimeError):
        await c.upload_post(tag_string="wolf", rating="e")  # neither file nor url
    await c.close()


# ── Poster happy path (creds via settings → default account) ─

@pytest.mark.asyncio
@respx.mock
async def test_poster_post_happy_path(tmp_path):
    config.save_settings({"e621_username": "u", "e621_api_key": "k"})
    img = tmp_path / "art.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
    respx.post("https://e621.net/uploads.json").mock(
        return_value=httpx.Response(200, json={
            "success": True, "location": "/posts/4242", "post_id": 4242}))

    poster = E621Poster()
    pkg = _pkg(["wolf", "male", "solo", "canine"], file_path=str(img), source="https://s")
    res = await poster.post(pkg)
    if poster._client:
        await poster._client.close()

    assert res.success is True
    assert res.external_id == "4242"
    assert res.external_url == "https://e621.net/posts/4242"
