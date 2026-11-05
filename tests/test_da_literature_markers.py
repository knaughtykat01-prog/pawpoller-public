"""DAMARKERS (4.88.9): DeviantArt literature never shows the archive's marker lines.

A story's Markdown carries whole-line ``<!-- @title -->``-style markers that steer the format
converters. Weasyl strips them before upload (``weasyl.strip_markers``); DeviantArt's three
literature paths (post, edit, replace file) sent the file as it was, so readers saw the markers.
"""
from __future__ import annotations

import asyncio

import pytest

from posting.platforms.base import StoryUploadPackage
from posting.platforms.deviantart import DeviantArtPoster

STORY = "<!-- @title -->\n# Sample Story\n\n<!-- @chapter 1 -->\nFirst line.\n\nSecond line.\n"


class _Client:
    def __init__(self):
        self.bodies = []

    async def oauth_create_literature(self, **kw):
        self.bodies.append(kw["body"])
        return {"deviationid": "u1", "url": "https://example.com/d/1"}

    async def oauth_update_literature(self, uuid, **kw):
        self.bodies.append(kw["body"])
        return {"deviationid": uuid, "url": "https://example.com/d/1"}

    async def uuid_for(self, external_id):
        return "u1"


@pytest.fixture
def setup(tmp_path, monkeypatch):
    path = tmp_path / "Sample_Story.md"
    path.write_text(STORY, encoding="utf-8")
    poster, client = DeviantArtPoster(), _Client()

    async def _ensure():
        return client, "tok"

    monkeypatch.setattr(poster, "_ensure_client", _ensure)
    pkg = StoryUploadPackage(
        story_name="Sample_Story", chapter_index=0, chapter_title="", platform="da",
        title="Sample Story", description="desc", tags=["a", "b"], rating="general",
        file_path=str(path), file_type="md")
    return poster, client, pkg


def _check(body):
    assert "<!--" not in body
    assert "First line." in body and "Second line." in body


def test_post_strips_markers(setup):
    poster, client, pkg = setup
    asyncio.run(poster.post(pkg))
    assert len(client.bodies) == 1
    _check(client.bodies[0])


def test_edit_strips_markers(setup):
    poster, client, pkg = setup
    asyncio.run(poster.edit("123", pkg))
    assert len(client.bodies) == 1
    _check(client.bodies[0])


def test_replace_file_strips_markers(setup):
    poster, client, pkg = setup
    asyncio.run(poster.replace_file("123", pkg.file_path))
    assert len(client.bodies) == 1
    _check(client.bodies[0])


def test_handoff_pull_says_it_strips_markers():
    """A newer server hands DeviantArt literature jobs only to desktops that send this token."""
    import inspect

    from routes import mirror_api
    assert '"supports": "marker_strip"' in inspect.getsource(mirror_api.handoff_pull)


def test_mirror_pull_says_it_strips_markers():
    """A newer server holds back stories with scene markers from a puller that doesn't send this header."""
    import inspect

    from routes import mirror_api
    assert mirror_api._PULL_SUPPORTS == {"X-PawPoller-Supports": "marker_strip"}
    for fn in (mirror_api._run_pull, mirror_api.compute_drift):
        assert "headers = {**_PULL_SUPPORTS," in inspect.getsource(fn), fn.__name__
