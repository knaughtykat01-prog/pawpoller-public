"""Editing a SoFurry artwork must not go looking for a story (3.9.5).

`post()` branches on the image file types before it reaches the chaptered-story
machinery. `edit()` did not, so editing a SoFurry *image* fell through to
`story_reader.load_story()` and raised

    FileNotFoundError: Story folder not found: /app/story-archive/Rear_View

observed on prod editing `Rear_View` (naOMVbXe) on 2026-08-19. The damage was
not the traceback: the exception propagated out and flipped a live, correctly
posted submission's publication row to `failed`.
"""
from __future__ import annotations

import pytest

from posting.platforms.base import StoryUploadPackage
from posting.platforms.sofurry import SoFurryPoster


def _package(file_type: str) -> StoryUploadPackage:
    return StoryUploadPackage(
        story_name="Rear_View", chapter_index=0, chapter_title="", platform="sf",
        title="Rear View", description="d", tags=["a"], rating="adult",
        file_path=f"/tmp/rear_view.{file_type}", file_type=file_type,
    )


class _FakeClient:
    def __init__(self):
        self.edited = False

    async def edit_submission(self, *a, **kw):
        self.edited = True
        return {"url": "https://sofurry.com/view/naOMVbXe"}

    async def get_content_ids(self, *a, **kw):        # pragma: no cover
        raise AssertionError("artwork edit must not touch story content")


@pytest.fixture
def poster(monkeypatch):
    p = SoFurryPoster()
    client = _FakeClient()

    async def _ensure():
        return client

    monkeypatch.setattr(p, "_ensure_client", _ensure)
    monkeypatch.setattr(p, "_cleanup_tmp_files", lambda: None)
    p._fake_client = client
    return p


@pytest.mark.asyncio
@pytest.mark.parametrize("ext", ["png", "jpg", "jpeg", "gif", "webp"])
async def test_editing_artwork_succeeds_without_loading_a_story(poster, ext, monkeypatch):
    """The failure mode that matters: an exception here marks a live submission
    failed. Any story lookup at all is the bug."""
    from posting import story_reader

    def _boom(name):
        raise AssertionError(f"edit() must not load a story for artwork (asked for {name!r})")

    monkeypatch.setattr(story_reader, "load_story", _boom)

    result = await poster.edit("naOMVbXe", _package(ext))

    assert result.success is True
    assert result.external_id == "naOMVbXe"
    assert poster._fake_client.edited, "the metadata edit must still happen"


@pytest.mark.asyncio
async def test_a_story_edit_still_loads_the_story(poster, monkeypatch):
    """The artwork guard must not swallow the story path it sits in front of."""
    from posting import story_reader

    called = {}

    def _load(name):
        called["name"] = name
        raise RuntimeError("stop here — reaching this proves the story path ran")

    monkeypatch.setattr(story_reader, "load_story", _load)

    result = await poster.edit("abc123", _package("bbcode"))

    assert called.get("name") == "Rear_View"
    assert result.success is False


@pytest.mark.asyncio
async def test_the_guard_matches_the_one_post_uses(poster):
    """post() and edit() must agree on what counts as an image, or an artwork
    that posts fine becomes an artwork that cannot be edited."""
    import inspect

    from posting.platforms import sofurry

    src = inspect.getsource(sofurry.SoFurryPoster)
    assert src.count('("png", "jpg", "jpeg", "gif", "webp")') >= 2, (
        "post() and edit() should share the same image-type test"
    )
