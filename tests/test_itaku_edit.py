"""Itaku gallery-image edits (3.35.0).

Itaku was marked `supports_edit = False` with the comment "Itaku does not
support editing via API" — the same unchecked claim e621 carried. Its own web
client edits an image through `app-edit-image-dialog`, which is
`PATCH /api/galleries/images/{id}/`: the DRF sibling of the `POST` the upload
already uses, taking the same field names on the same token.

Three properties are asserted here rather than left to comments:

1. **share_on_feed is never sent.** The upload sets it and Itaku reads it as
   "announce to my followers". On an edit it would re-share the piece to every
   follower's feed on each metadata sync.
2. **Gallery images only.** `post()` also creates text posts via a different
   endpoint; letting a text package reach the image endpoint is the DeviantArt
   3.34.0 crash in a different costume.
3. **The 5-tag floor holds.** Itaku rejects a thinner set, and in a replace it
   would strip a well-tagged live image.
"""
from __future__ import annotations

import json

import pytest

from clients.ik.client import IKClient
from posting.platforms.base import StoryUploadPackage
from posting.platforms.itaku import ItakuPoster


class _FakeResponse:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text

    def json(self):
        return {"id": 123}


class _FakeHTTP:
    def __init__(self, response: _FakeResponse | None = None):
        self.calls: list[dict] = []
        self.response = response or _FakeResponse()

    async def patch(self, url, data=None, headers=None, timeout=None):
        self.calls.append({"url": url, "data": dict(data or {}), "headers": headers})
        return self.response

    @property
    def last(self) -> dict:
        assert self.calls, "no PATCH was sent"
        return self.calls[-1]["data"]


def _client(response: _FakeResponse | None = None) -> IKClient:
    c = IKClient("someuser")
    c._http = _FakeHTTP(response)
    return c


def _package(**over) -> StoryUploadPackage:
    kw = dict(
        story_name="Some_Piece", chapter_index=0, chapter_title="",
        platform="ik", title="A Canonical Title", description="a caption",
        tags=["alpha", "beta", "gamma", "delta", "epsilon"], rating="mature",
        file_path="/tmp/piece.png", file_type="png",
    )
    kw.update(over)
    return StoryUploadPackage(**kw)


def _poster(client, monkeypatch) -> ItakuPoster:
    p = ItakuPoster()

    async def _ensure():
        return client, "tok123"

    monkeypatch.setattr(p, "_ensure_client", _ensure)
    return p


# ── the capability ───────────────────────────────────────────────────────────

def test_itaku_declares_it_can_edit():
    assert ItakuPoster.supports_edit is True
    assert ItakuPoster.supports_artwork_edit is True
    assert ItakuPoster.supports_file_replace is False, (
        "Itaku's dialog offers it, but it is not built — a flag must not overstate"
    )


# ── the follower-spam trap ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_an_edit_never_reshares_to_the_feed():
    """share_on_feed on an edit pushes the piece back onto every follower's
    activity feed — on every routine metadata sync."""
    c = _client()
    await c.edit_image(123, title="t", token="tok")
    assert "share_on_feed" not in c._http.last


# ── the endpoint and its shape ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_it_patches_the_gallery_image_endpoint():
    c = _client()
    await c.edit_image(123, title="t", token="tok")
    assert c._http.calls[0]["url"] == "https://itaku.ee/api/galleries/images/123/"
    assert c._http.calls[0]["headers"]["Authorization"].endswith("tok")


@pytest.mark.asyncio
async def test_tags_go_up_in_itakus_object_shape():
    c = _client()
    await c.edit_image(123, tags=["a", "b", "c", "d", "e"], token="tok")
    assert json.loads(c._http.last["tags"]) == [
        {"name": "a"}, {"name": "b"}, {"name": "c"}, {"name": "d"}, {"name": "e"},
    ]


@pytest.mark.asyncio
async def test_only_the_fields_passed_are_sent():
    """DRF PATCH is a partial update — an unsent field keeps its current value,
    so a caller with no folders cannot wipe the ones on the image."""
    c = _client()
    await c.edit_image(123, title="just the title", token="tok")
    assert set(c._http.last) == {"title"}


@pytest.mark.asyncio
async def test_nothing_to_change_sends_no_request():
    c = _client()
    out = await c.edit_image(123, token="tok")
    assert out["unchanged"] is True
    assert not c._http.calls


@pytest.mark.asyncio
async def test_the_title_is_capped_at_itakus_own_limit():
    c = _client()
    await c.edit_image(123, title="x" * 200, token="tok")
    assert len(c._http.last["title"]) == 100


@pytest.mark.asyncio
async def test_a_bad_maturity_rating_is_refused_before_sending():
    c = _client()
    with pytest.raises(RuntimeError, match="SFW/Questionable/NSFW"):
        await c.edit_image(123, maturity_rating="Explicit", token="tok")
    assert not c._http.calls


@pytest.mark.asyncio
async def test_an_edit_without_a_token_is_refused():
    c = _client()
    with pytest.raises(RuntimeError, match="token required"):
        await c.edit_image(123, title="t")
    assert not c._http.calls


# ── the tag floor ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_too_few_tags_is_refused_by_the_client():
    c = _client()
    with pytest.raises(RuntimeError, match="at least 5 tags"):
        await c.edit_image(123, tags=["a", "b"], token="tok")
    assert not c._http.calls


@pytest.mark.asyncio
async def test_too_few_tags_is_a_clean_failed_result_from_the_poster(monkeypatch):
    """An exception out of edit() flips a live publication row to failed."""
    c = _client()
    p = _poster(c, monkeypatch)
    result = await p.edit("123", _package(tags=["a", "b"]))
    assert result.success is False
    assert "at least 5 tags" in (result.error or "")
    assert result.external_id == "123"
    assert not c._http.calls


# ── content-type discipline ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_text_package_does_not_reach_the_image_endpoint(monkeypatch):
    """post() routes non-images to create_post; edit() must not silently send
    one to the gallery-image endpoint."""
    c = _client()
    p = _poster(c, monkeypatch)
    result = await p.edit("123", _package(file_type="txt", file_path="/tmp/p.txt"))
    assert result.success is False
    assert "gallery images only" in (result.error or "")
    assert not c._http.calls


def test_post_and_edit_share_one_image_test():
    import inspect

    from posting.platforms import itaku

    src = inspect.getsource(itaku)
    assert src.count("_IMAGE_TYPES") >= 3, "post() and edit() must test one constant"
    assert itaku._IMAGE_TYPES == ("png", "jpg", "jpeg", "gif", "webp")


# ── the happy path ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_full_metadata_edit(monkeypatch):
    c = _client()
    p = _poster(c, monkeypatch)

    pkg = _package()
    pkg.extra["skip_content_refresh"] = True     # Sync-all sets this on every member
    result = await p.edit("123", pkg)

    assert result.success is True
    assert result.external_url == "https://itaku.ee/image/123"
    sent = c._http.last
    assert sent["title"] == "A Canonical Title"
    assert sent["description"] == "a caption"
    assert sent["maturity_rating"] == "Questionable"      # 'mature' maps here
    assert len(json.loads(sent["tags"])) == 5


@pytest.mark.asyncio
async def test_a_rejection_comes_back_as_a_failed_result(monkeypatch):
    c = _client(_FakeResponse(400, text="tags: at least 5 required"))
    p = _poster(c, monkeypatch)
    result = await p.edit("123", _package())
    assert result.success is False
    assert "400" in (result.error or "")
