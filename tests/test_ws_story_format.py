"""Weasyl stories go up as Markdown (4.54.3).

Weasyl takes .txt and .md for a literary submission and renders the Markdown; the BBCode .txt
PawPoller used to send showed as plain text with the formatting lost. The archive's
``<!-- @marker -->`` lines steer the converters and are stripped from the uploaded copy.
"""
from __future__ import annotations

import os

from posting.platforms import weasyl
from posting.story_reader import PLATFORM_FORMAT_MAP


def test_weasyl_prefers_chapter_markdown_then_master_and_never_bbcode():
    specs = PLATFORM_FORMAT_MAP["ws"]
    assert [s[:2] for s in specs] == [("Chapters/Markdown", "*.md"), ("Markdown", "MASTER.md")]
    assert all(s[2] == "markdown" for s in specs)


def test_the_cover_goes_as_cover_and_gallery_thumbnail(tmp_path):
    """Weasyl kept the cover but showed its default thumbnail: it makes none from a cover."""
    import asyncio
    from clients.weasyl.client import WeasylClient
    sent = {}

    class _Resp:
        status_code, url, text = 200, "https://www.weasyl.com/~sample/submissions/5/s", ""

    class _HTTP:
        async def post(self, url, data=None, files=None, timeout=None, follow_redirects=False):
            sent.update({k: (v[0], v[2] if len(v) > 2 else None) for k, v in files.items()})
            return _Resp()

    c = WeasylClient.__new__(WeasylClient)
    c._http = _HTTP()

    async def _csrf(url):
        return ""
    c._get_csrf_token = _csrf
    story, cover = tmp_path / "s.md", tmp_path / "cover.jpg"
    story.write_text("# S\n", encoding="utf-8")
    cover.write_bytes(b"\xff\xd8\xff")
    cropped = []

    async def _crop(sid):
        cropped.append(sid)
    c.thumbnail_from_cover = _crop
    asyncio.run(c.submit_literary(str(story), title="S", tags="a b", cover_path=str(cover)))
    assert sent["coverfile"] == sent["thumbfile"] == ("cover.jpg", "image/jpeg")
    assert cropped == ["5"]        # the upload alone leaves Weasyl's default thumbnail


def test_markers_are_stripped_and_the_story_kept():
    src = ("<!-- @title -->\n# Sample Story\n\n<!-- @subtitle -->\n*A subtitle*\n\n\n"
           "<!-- @byline -->\nBy KnaughtyKat\n\n*Italic narration.* \"Dialogue.\"\n")
    out = weasyl.strip_markers(src)
    assert "<!--" not in out
    assert out == "# Sample Story\n\n*A subtitle*\n\nBy KnaughtyKat\n\n*Italic narration.* \"Dialogue.\"\n"


def test_the_upload_is_a_same_named_temp_copy_and_the_archive_is_untouched(tmp_path):
    story = tmp_path / "Chapter_1_Sample.md"
    story.write_text("<!-- @title -->\n# T\n\nBody\n", encoding="utf-8")
    with weasyl._publishable(str(story)) as up:
        assert os.path.basename(up) == "Chapter_1_Sample.md" and up != str(story)
        assert open(up, encoding="utf-8").read() == "# T\n\nBody\n"
    assert not os.path.exists(up)
    assert story.read_text(encoding="utf-8").startswith("<!-- @title -->")
    txt = tmp_path / "s.txt"
    txt.write_text("x", encoding="utf-8")
    with weasyl._publishable(str(txt)) as up:
        assert up == str(txt)
