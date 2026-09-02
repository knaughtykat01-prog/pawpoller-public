"""DeviantArt descriptions and artwork edits (3.36.0).

3.34.0 concluded that DA had no image-edit endpoint and made Sync skip artwork.
That was wrong: OUR CLIENT had none. DA exposes `POST /deviation/edit/{id}` for
any deviation type — but it carries **no description parameter**, so the
description goes through the editor's own
`POST /_napi/shared_api/deviation/update` with a structured `editorRaw` document.

Captured from the live editor on 2026-09-02, on a real deviation whose
description DA's own editor refused to open:

    Invalid Input — Some content in your document cannot be processed by the
    editor. You can attempt to recover your document, but some text or
    formatting may be lost.

The text rendered on the page perfectly well; what was broken was HAND-EDITING.
A description of bare newlines has no block elements for the editor to map onto
paragraphs, so it will not deserialise. Posting paragraph HTML fixes that at
source.
"""
from __future__ import annotations

import json

import pytest

from clients.da.client import (
    _split_paragraphs,
    description_to_da_html,
    description_to_editor_raw,
)

SAMPLE = (
    "The question in the caption is rhetorical.\n"
    "\n"
    "Art by Someone - https://www.deviantart.com/someone\n"
    "\n"
    "\U0001F43E Posted via PawPoller"
)


# ── paragraph splitting ──────────────────────────────────────────────────────

def test_blank_lines_become_one_empty_paragraph():
    assert _split_paragraphs("a\n\n\n\nb") == ["a", "", "b"]


def test_trailing_blank_lines_are_dropped():
    assert _split_paragraphs("a\n\n\n") == ["a"]


def test_crlf_is_handled():
    assert _split_paragraphs("a\r\n\r\nb") == ["a", "", "b"]


def test_empty_text_yields_nothing():
    assert _split_paragraphs("") == []
    assert description_to_da_html("") == ""


# ── the HTML DA stores ───────────────────────────────────────────────────────

def test_html_uses_the_exact_style_da_renders():
    """Measured from a live deviation's rendered description."""
    html = description_to_da_html("hello")
    assert html == '<p style="text-align: left; margin-inline-start: 0px;">hello</p>'


def test_every_paragraph_is_a_block_element():
    """The actual fix: bare newlines are what the editor cannot parse."""
    html = description_to_da_html(SAMPLE)
    assert html.count("<p ") == 5          # 3 paragraphs + 2 blank separators
    assert "\n" not in html


def test_html_is_escaped():
    html = description_to_da_html('a <b> & "c"')
    assert "&lt;b&gt;" in html and "&amp;" in html
    assert "<b>" not in html


# ── the editorRaw document ───────────────────────────────────────────────────

def test_editor_raw_is_a_json_string_not_an_object():
    """The endpoint's `editorRaw` value is itself JSON — double-encoded."""
    raw = description_to_editor_raw(SAMPLE)
    assert isinstance(raw, str)
    doc = json.loads(raw)
    assert doc["version"] == "1"
    assert doc["document"]["type"] == "doc"


def test_paragraph_nodes_match_the_captured_shape():
    doc = json.loads(description_to_editor_raw("hello"))
    node = doc["document"]["content"][0]
    assert node["type"] == "paragraph"
    assert node["attrs"] == {"indentType": None, "indentation": "", "textAlign": "left"}
    assert node["content"] == [{"type": "text", "text": "hello"}]


def test_a_blank_line_is_a_paragraph_with_no_content_key():
    """Exactly how the live editor serialises an empty line — an empty
    `content` list is a different thing and does not round-trip."""
    doc = json.loads(description_to_editor_raw("a\n\nb"))
    nodes = doc["document"]["content"]
    assert len(nodes) == 3
    assert "content" not in nodes[1]
    assert nodes[1]["attrs"]["textAlign"] == "left"


def test_unicode_survives_unescaped():
    """Em dashes and emoji are the characters most likely to be mangled."""
    raw = description_to_editor_raw("dash — and \U0001F43E paw")
    assert "—" in raw and "\U0001F43E" in raw
    doc = json.loads(raw)
    assert doc["document"]["content"][0]["content"][0]["text"] == "dash — and \U0001F43E paw"


def test_the_two_renderings_agree_on_paragraph_count():
    """HTML (post time) and editorRaw (edit time) must describe one document."""
    html_paras = description_to_da_html(SAMPLE).count("<p ")
    raw_paras = len(json.loads(description_to_editor_raw(SAMPLE))["document"]["content"])
    assert html_paras == raw_paras


# ── the poster ───────────────────────────────────────────────────────────────

def test_da_can_edit_artwork_again():
    from posting.platforms.deviantart import DeviantArtPoster
    assert DeviantArtPoster.supports_artwork_edit is True, (
        "3.34.0 set this False on the wrong premise — DA has /deviation/edit/{id}"
    )


@pytest.mark.asyncio
async def test_artwork_edit_sends_metadata_and_description(monkeypatch):
    from posting.platforms.base import StoryUploadPackage
    from posting.platforms.deviantart import DeviantArtPoster

    calls = {}

    class _Client:
        cookie_value = "sessionid=x"

        async def uuid_for(self, ext):
            return f"uuid-{ext}"

        async def oauth_edit_deviation(self, dev_id, **kw):
            calls["edit"] = {"id": dev_id, **kw}
            return {}

        async def napi_set_description(self, ext_id, text, csrf_token=""):
            calls["desc"] = {"id": ext_id, "text": text}
            return {}

    p = DeviantArtPoster()

    async def _ensure():
        return _Client(), "tok"

    monkeypatch.setattr(p, "_ensure_client", _ensure)
    monkeypatch.setattr(p, "_deviation_url", lambda *a, **kw: "https://da/x")

    pkg = StoryUploadPackage(
        story_name="P", chapter_index=0, chapter_title="", platform="da",
        title="T", description="body text", tags=["a", "b"], rating="adult",
        file_path="/tmp/p.png", file_type="png")
    pkg.extra["skip_content_refresh"] = True

    result = await p.edit("123", pkg)

    assert result.success is True
    assert calls["edit"]["id"] == "uuid-123"
    assert calls["edit"]["title"] == "T"
    assert calls["edit"]["is_mature"] is True
    assert calls["desc"] == {"id": "123", "text": "body text"}


@pytest.mark.asyncio
async def test_a_missing_cookie_does_not_fail_the_whole_edit(monkeypatch):
    """Title/tags/rating go through OAuth and must still land; the description
    is the only part that needs the cookie."""
    from posting.platforms.base import StoryUploadPackage
    from posting.platforms.deviantart import DeviantArtPoster

    class _Client:
        async def uuid_for(self, ext):
            return ext

        async def oauth_edit_deviation(self, dev_id, **kw):
            return {}

        async def napi_set_description(self, ext_id, text, csrf_token=""):
            raise RuntimeError("DA: setting a description needs the da_cookie")

    p = DeviantArtPoster()

    async def _ensure():
        return _Client(), "tok"

    monkeypatch.setattr(p, "_ensure_client", _ensure)
    monkeypatch.setattr(p, "_deviation_url", lambda *a, **kw: "https://da/x")

    pkg = StoryUploadPackage(
        story_name="P", chapter_index=0, chapter_title="", platform="da",
        title="T", description="body", tags=["a"], rating="general",
        file_path="/tmp/p.png", file_type="png")

    result = await p.edit("123", pkg)

    assert result.success is True, "an unreachable description must not fail the edit"
    assert "Description NOT changed" in (result.error or "")
    assert "da_cookie" in (result.error or "")
