"""Furbooru posting (4.28.0): the tag set the rulebook wants, the DNP gate, the
rating tag, and the client's upload call (network faked)."""

import httpx
import pytest
import respx

from clients.fbr.client import FurbooruClient
from posting.platforms.base import StoryUploadPackage
from posting.platforms.furbooru import (FurbooruPoster, build_tag_list, dnp_refusal,
                                        rating_tag, to_site_tag)

BASE = "https://furbooru.org"


def _pkg(tags, rating="adult", file_path="/tmp/x.png", extra=None):
    return StoryUploadPackage(
        story_name="Art", chapter_index=0, chapter_title="", platform="fbr",
        title="Art", description="desc", tags=tags, rating=rating,
        file_path=file_path, extra=extra or {},
    )


@pytest.mark.parametrize("rating,expected", [
    ("general", "safe"), ("safe", "safe"), ("mature", "questionable"),
    ("questionable", "questionable"), ("adult", "explicit"), ("", "safe"), ("garbage", "explicit"),
])
def test_rating_tag(rating, expected):
    assert rating_tag(rating) == expected


def test_site_tags_use_spaces_and_keep_namespaces():
    assert to_site_tag("solo_female") == "solo female"
    assert to_site_tag("artist:inkwolf") == "artist:inkwolf"
    assert to_site_tag("  Big__Cat ") == "big cat"


def test_tag_list_puts_rating_first_and_namespaces_the_artist():
    # artwork_reader injects the bare artist name first; extra carries the display name
    tags = build_tag_list(_pkg(["inkwolf", "wolf", "male", "solo_male", "canine"],
                               extra={"artist_name": "Inkwolf"}))
    assert tags == ["explicit", "artist:inkwolf", "wolf", "male", "solo male", "canine"]


def test_tag_list_marks_artist_needed_when_there_is_none():
    tags = build_tag_list(_pkg(["wolf", "male", "solo", "canine"]))
    assert tags[0] == "explicit" and tags[-1] == "artist needed"


def test_tag_list_drops_a_rating_tag_the_catalogue_carried():
    tags = build_tag_list(_pkg(["safe", "wolf", "male", "solo", "canine"], rating="adult"))
    assert tags.count("explicit") == 1 and "safe" not in tags


def test_validate_enforces_the_five_tag_floor_and_a_file(tmp_path):
    p = FurbooruPoster()
    f = tmp_path / "a.png"
    f.write_bytes(b"\x89PNG" + b"0" * 16)
    assert any("5 or more" in e for e in p.validate(_pkg(["wolf", "male"], file_path=str(f))))
    assert any("image file" in e for e in p.validate(_pkg(["wolf", "male", "solo", "canine"], file_path="")))
    assert p.validate(_pkg(["wolf", "male", "solo", "canine"], file_path=str(f))) == []   # + rating = 5


# ── DNP gate ─────────────────────────────────────────────────────────────

def _tag_search(entries):
    return httpx.Response(200, json={"tags": [{"name": "artist:inkwolf", "dnp_entries": entries}]})


@pytest.mark.asyncio
@respx.mock
async def test_dnp_artist_upload_only_refuses_with_the_conditions():
    respx.get(f"{BASE}/api/v1/json/search/tags").mock(return_value=_tag_search([
        {"dnp_type": "Artist Upload Only", "conditions": "Only I upload my art."}]))
    c = FurbooruClient(username="u", api_key="k")
    msg = await dnp_refusal(c, ["explicit", "artist:inkwolf", "wolf"])
    await c.close()
    assert "Artist Upload Only" in msg and "Only I upload my art." in msg and "dnp_ack" in msg


@pytest.mark.asyncio
@respx.mock
async def test_dnp_no_edits_does_not_block():
    respx.get(f"{BASE}/api/v1/json/search/tags").mock(return_value=_tag_search([
        {"dnp_type": "No Edits", "conditions": "No NSFW edits."}]))
    c = FurbooruClient(username="u", api_key="k")
    assert await dnp_refusal(c, ["explicit", "artist:inkwolf", "wolf"]) == ""
    await c.close()


@pytest.mark.asyncio
@respx.mock
async def test_dnp_unlisted_artist_passes():
    respx.get(f"{BASE}/api/v1/json/search/tags").mock(
        return_value=httpx.Response(200, json={"tags": []}))
    c = FurbooruClient(username="u", api_key="k")
    assert await dnp_refusal(c, ["explicit", "artist:inkwolf"]) == ""
    await c.close()


# ── Upload ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
@respx.mock
async def test_upload_image_success(tmp_path):
    img = tmp_path / "art.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
    route = respx.post(f"{BASE}/api/v1/json/images").mock(
        return_value=httpx.Response(200, json={"image": {"id": 4242}}))
    c = FurbooruClient(username="u", api_key="k")
    res = await c.upload_image(file_path=str(img), tag_input="explicit, artist:inkwolf, wolf",
                               sources=["https://example.com/post/1"])
    await c.close()
    assert res == {"image_id": "4242", "url": f"{BASE}/images/4242"}
    req = route.calls[0].request
    assert req.url.params["key"] == "k"
    body = req.content
    assert b'name="image[image]"' in body and b'name="image[tag_input]"' in body
    assert b'name="image[sources][0][source]"' in body


@pytest.mark.asyncio
@respx.mock
async def test_upload_image_rejection_carries_the_site_message(tmp_path):
    img = tmp_path / "art.png"
    img.write_bytes(b"x")
    respx.post(f"{BASE}/api/v1/json/images").mock(
        return_value=httpx.Response(400, json={"errors": {"tag_input": ["must have at least 5 tags"]}}))
    c = FurbooruClient(username="u", api_key="k")
    with pytest.raises(RuntimeError) as ei:
        await c.upload_image(file_path=str(img), tag_input="safe")
    await c.close()
    assert "at least 5 tags" in str(ei.value)


@pytest.mark.asyncio
async def test_upload_needs_an_api_key(tmp_path):
    c = FurbooruClient(username="u", api_key="")
    with pytest.raises(RuntimeError) as ei:
        await c.upload_image(file_path=str(tmp_path / "nope.png"), tag_input="safe")
    assert "API key" in str(ei.value)


@pytest.mark.asyncio
@respx.mock
async def test_poster_refuses_on_dnp_before_uploading(tmp_path, monkeypatch):
    import config
    monkeypatch.setattr(config, "get_settings", lambda: {"fbr_username": "u", "fbr_api_key": "k"})
    img = tmp_path / "a.png"
    img.write_bytes(b"x")
    respx.get(f"{BASE}/api/v1/json/search/tags").mock(return_value=_tag_search([
        {"dnp_type": "With Permission Only", "conditions": "Ask first."}]))
    upload = respx.post(f"{BASE}/api/v1/json/images").mock(
        return_value=httpx.Response(200, json={"image": {"id": 1}}))
    p = FurbooruPoster()
    res = await p.post(_pkg(["inkwolf", "wolf", "male", "solo", "canine"], file_path=str(img),
                            extra={"artist_name": "Inkwolf"}))
    assert res.success is False and "With Permission Only" in (res.error or "")
    assert not upload.called
    # the acknowledgement lets it through
    res = await p.post(_pkg(["inkwolf", "wolf", "male", "solo", "canine"], file_path=str(img),
                            extra={"artist_name": "Inkwolf", "dnp_ack": True}))
    assert res.success is True and res.external_url == f"{BASE}/images/1"
