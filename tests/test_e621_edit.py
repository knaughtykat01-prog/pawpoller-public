"""e621 metadata edits (3.33.0).

e621 was marked ``supports_edit = False`` on the strength of a spec note
saying it needed "a separate tag-edit API". It does not — editing a post is an
ordinary ``PATCH /posts/{id}.json`` taking the same HTTP Basic username + API
key the client already uses to poll, upload and comment.

Two properties of e621 make its edit different from every other gallery
PawPoller writes to, and both are asserted here rather than left to a comment:

1. **There is no title.** The post model is tags + rating + description +
   sources + parent. A title sent anywhere would either be dropped by e621 or,
   worse, smuggled into the description and overwrite the caption on every
   sync.
2. **Tags are communal.** Janitors and other users retag posts. A metadata
   sync that pushed our canonical set as an overwrite would delete their work
   silently, which is what spec 0-A1 forbids, so the default is a merge.
"""
from __future__ import annotations

import pytest

from clients.e621.client import E621Client
from posting.platforms.base import StoryUploadPackage
from posting.platforms.e621 import E621Poster


# The shape GET /posts/{id}.json returns, including the
# category split e621 returns tags in and a tag we did not put there.
LIVE_POST = {
    "id": 1234567,
    "rating": "q",
    "description": "a caption\n\nArt by \"SomeArtist\":https://example.com/u/someartist",
    "sources": [],
    "tags": {
        "general": ["anthro", "solo", "sitting", "janitor_added_tag"],
        "artist": [],
        "contributor": [],
        "copyright": [],
        "character": ["somechar_(someartist)"],
        "species": ["tiger", "mammal"],
        "invalid": [],
        "meta": ["hi_res"],
        "lore": [],
    },
    "relationships": {"parent_id": None},
}


class _FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text
        self.content = b"{}"

    def json(self):
        return self._payload


class _FakeHTTP:
    """Records the PATCH so the test can assert on the exact form fields."""

    def __init__(self, response: _FakeResponse | None = None):
        self.calls: list[dict] = []
        self.response = response or _FakeResponse()

    async def patch(self, url, data=None, headers=None, auth=None, timeout=None):
        self.calls.append({"url": url, "data": dict(data or {}), "auth": auth})
        return self.response

    @property
    def last(self) -> dict:
        assert self.calls, "no PATCH was sent"
        return self.calls[-1]["data"]


def _client(response: _FakeResponse | None = None,
            current: dict | None = LIVE_POST) -> E621Client:
    c = E621Client(username="someone", api_key="key")
    c._http = _FakeHTTP(response)

    async def _get_post(post_id):
        return current

    c.get_post = _get_post          # type: ignore[method-assign]
    return c


def _package(**over) -> StoryUploadPackage:
    kw = dict(
        story_name="Some_Piece", chapter_index=0, chapter_title="",
        platform="e621", title="A Canonical Title",
        description="new caption", tags=["anthro", "solo", "tiger", "sparkles"],
        rating="mature", file_path=None, file_type="png",
    )
    kw.update(over)
    return StoryUploadPackage(**kw)


# ── the capability itself ────────────────────────────────────────────────────

def test_e621_declares_it_can_edit():
    """A silent revert to False would make Sync-all skip e621 as post-only
    again, with no test failing anywhere."""
    assert E621Poster.supports_edit is True
    assert E621Poster.supports_file_replace is False, (
        "a new image on e621 is a new post, not a replacement"
    )


# ── no title anywhere ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_title_is_never_sent():
    """e621's post model has no title. Sending one is at best ignored; folding
    it into the description would rewrite the caption on every sync."""
    poster = E621Poster()
    client = _client()

    async def _ensure():
        return client

    poster._ensure_client = _ensure                # type: ignore[method-assign]

    result = await poster.edit("1234567", _package())

    assert result.success is True
    sent = client._http.last
    assert not any("title" in key for key in sent), sent
    assert "A Canonical Title" not in " ".join(str(v) for v in sent.values()), (
        "the canonical title must not be smuggled into another field"
    )
    assert sent["post[description]"] == "new caption"


# ── the three-way merge fields ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_every_changed_field_carries_its_old_value():
    """e621 reconciles concurrent edits from these. Sending a new value with no
    old value turns a merge into a blind overwrite."""
    client = _client()
    await client.edit_post("1234567", tags=["anthro", "solo"], rating="e",
                           description="d", sources=["https://example.com/x"])

    sent = client._http.last
    for field in ("tag_string", "rating", "description", "source"):
        assert f"post[{field}]" in sent
        assert f"post[old_{field}]" in sent, f"{field} was sent without its old value"
    assert sent["post[old_rating]"] == "q"
    assert sent["post[old_description]"] == LIVE_POST["description"]
    assert sent["post[old_source]"] == ""


@pytest.mark.asyncio
async def test_an_unreadable_post_is_not_edited_blind():
    client = _client(current=None)
    with pytest.raises(RuntimeError, match="refusing to edit blind"):
        await client.edit_post("1234567", tags=["anthro", "solo"])
    assert not client._http.calls


# ── tags are communal ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_merge_keeps_tags_we_did_not_put_there():
    """The default. A janitor's tag surviving our sync is the whole point."""
    client = _client()
    await client.edit_post("1234567", tags=["sparkles", "anthro"])

    final = client._http.last["post[tag_string]"].split()
    assert "janitor_added_tag" in final
    assert "somechar_(someartist)" in final
    assert "sparkles" in final, "our new tag must still be added"
    assert final.count("anthro") == 1, "a tag on both sides must not double up"


@pytest.mark.asyncio
async def test_replace_mode_sends_exactly_our_set():
    client = _client()
    await client.edit_post("1234567", tags=["sparkles", "anthro"], tag_mode="replace")

    final = client._http.last["post[tag_string]"].split()
    assert final == ["sparkles", "anthro"]
    assert "janitor_added_tag" not in final


@pytest.mark.asyncio
async def test_an_empty_tag_set_is_refused():
    """A blank tag_string reads as 'remove every tag' — it would gut the post."""
    client = _client()
    with pytest.raises(RuntimeError, match="empty tag set"):
        await client.edit_post("1234567", tags=["   ", ""], tag_mode="replace")
    assert not client._http.calls


@pytest.mark.asyncio
async def test_tags_are_normalised_to_e621_form():
    client = _client()
    await client.edit_post("1234567", tags=["Rim Lighting"], tag_mode="replace")
    assert client._http.last["post[tag_string]"] == "rim_lighting"


# ── not touching what we were not asked to touch ─────────────────────────────

@pytest.mark.asyncio
async def test_sources_are_left_alone_when_none_are_supplied():
    """Passing nothing must not clear the sources already on the post."""
    client = _client()
    await client.edit_post("1234567", tags=["anthro", "solo"])
    assert "post[source]" not in client._http.last


@pytest.mark.asyncio
async def test_an_edit_reason_is_always_sent():
    """It shows on the post's version history; an unexplained bulk retag is
    what draws a janitor's attention."""
    client = _client()
    await client.edit_post("1234567", tags=["anthro", "solo"])
    assert client._http.last["post[edit_reason]"]


@pytest.mark.asyncio
async def test_nothing_to_change_sends_no_request():
    client = _client()
    out = await client.edit_post("1234567")
    assert out["unchanged"] is True
    assert not client._http.calls


# ── the poster's guards ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_thin_tag_set_is_refused_without_raising():
    """An exception out of edit() flips a live publication row to failed
    (the 3.9.5 SoFurry lesson) — this must be a clean failed PostResult."""
    poster = E621Poster()
    client = _client()

    async def _ensure():
        return client

    poster._ensure_client = _ensure                # type: ignore[method-assign]

    result = await poster.edit("1234567", _package(tags=["anthro", "solo"]))

    assert result.success is False
    assert "at least 4" in (result.error or "")
    assert result.external_id == "1234567"
    assert not client._http.calls


@pytest.mark.asyncio
async def test_a_rejection_comes_back_as_a_failed_result():
    poster = E621Poster()
    client = _client(_FakeResponse(422, {"reason": "tag validation failed"}))

    async def _ensure():
        return client

    poster._ensure_client = _ensure                # type: ignore[method-assign]

    result = await poster.edit("1234567", _package())

    assert result.success is False
    assert "tag validation failed" in (result.error or "")


@pytest.mark.asyncio
async def test_sync_all_style_extras_do_not_break_the_edit():
    """update_artwork sets skip_content_refresh on every member. e621 has no
    content to refresh, so it must be accepted and ignored, not choke."""
    poster = E621Poster()
    client = _client()

    async def _ensure():
        return client

    poster._ensure_client = _ensure                # type: ignore[method-assign]

    pkg = _package()
    pkg.extra["skip_content_refresh"] = True
    result = await poster.edit("1234567", pkg)

    assert result.success is True


@pytest.mark.asyncio
async def test_the_rating_mapping_matches_the_upload_path():
    """A rating that differs between post() and edit() would silently reclassify
    a piece on every sync."""
    poster = E621Poster()
    client = _client()

    async def _ensure():
        return client

    poster._ensure_client = _ensure                # type: ignore[method-assign]

    for rating, expected in (("general", "s"), ("mature", "q"), ("adult", "e"),
                             ("anything-unknown", "e")):
        await poster.edit("1234567", _package(rating=rating))
        assert client._http.last["post[rating]"] == expected, rating
