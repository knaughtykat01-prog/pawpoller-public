"""Per-item Telegram options, and the story links that were never populated.

4.0.5 added the options and channel-wide defaults, but the only way to set one
on a single piece was to hand-edit its metadata file. "Possible" is not
"customisable", so the artwork edit form now carries the controls.

Two things worth pinning:

1. **Tri-state, not a checkbox.** "Default" (follow the channel) is a distinct
   state and the common one. A checkbox collapses it into whichever bool the
   channel default happened to be when the piece was saved, silently freezing
   it against later changes to the default.
2. **The save merges.** ``categories`` holds every platform's submission
   parameters — FA's category, Inkbunny's type. Writing only ``tg`` would wipe
   the rest.

And a bug this surfaced: the story package passed no ``extra`` at all, so the
Telegram poster read ``extra['links']`` and always found it empty. Story
announcements had been going out with no links while the release notes claimed
otherwise.
"""
from __future__ import annotations

import pytest


class TestArtworkOptionsUI:
    def test_the_edit_form_offers_the_options(self):
        # The live form is the Masterpiece page's platform panels, drawn by
        # artwork.js's _renderPlatformRows (the pre-2.193 edit form that held
        # its own <details> block was deleted in 4.5.0).
        js = open("frontend/js/artwork.js", encoding="utf-8").read()
        mp = open("frontend/js/masterpieces.js", encoding="utf-8").read()
        assert "Artwork._renderPlatformRows(" in mp, "the Masterpiece page does not draw the platform panels"
        assert "this._tgOptRows(" in js[js.index("_renderPlatformRows(el"):], "no options rows in the platform panels"
        assert "_tgOptRows" in js and "_collectTgOpts" in js

    def test_every_backend_option_has_a_control(self):
        """A backend option with no control is invisible; a control with no
        backend option does nothing. Both lists have to agree."""
        js = open("frontend/js/artwork.js", encoding="utf-8").read()
        py = open("posting/platforms/telegram.py", encoding="utf-8").read()
        start = py.index("def _resolve_options")
        block = py[start:py.index("def _build_caption")]
        backend = {k for k in ("spoiler", "tags", "caption", "protect",
                               "document", "silent", "pin", "preview")
                   if f'"{k}":' in block}
        ui_start = js.index("_TG_OPTS:")
        ui_block = js[ui_start:ui_start + 2000]
        # No exemptions. An earlier version excluded `preview` here with no
        # recorded reason, which is how the one option the backend supported
        # and neither UI offered stayed invisible for four releases (4.0.11).
        missing = sorted(k for k in backend if f"'{k}'" not in ui_block)
        assert not missing, f"backend options with no UI control: {missing}"

    def test_the_control_is_tri_state(self):
        """Default / On / Off. A checkbox cannot say 'follow the channel'."""
        js = open("frontend/js/artwork.js", encoding="utf-8").read()
        i = js.index("_tgOptRows(opts, extra) {")   # the definition, not the call site
        block = js[i:i + 1200]
        assert ">Default<" in block
        assert ">On<" in block and ">Off<" in block

    def test_only_explicit_choices_are_stored(self):
        """An untouched option must stay ABSENT, so it keeps following the
        channel default rather than being frozen at today's value."""
        js = open("frontend/js/artwork.js", encoding="utf-8").read()
        i = js.index("_collectTgOpts()")
        block = js[i:i + 500]
        assert "=== 'on'" in block and "=== 'off'" in block, (
            "collector must record only explicit on/off, never the empty default")

    def test_saving_merges_rather_than_replaces_categories(self):
        """categories carries every platform's params. Replacing it with just
        tg would wipe FA's category and Inkbunny's type."""
        js = open("frontend/js/artwork.js", encoding="utf-8").read()
        i = js.index("async _saveMeta(name, data) {")   # ditto
        block = js[i:i + 1500]
        assert "...(data.categories || {})" in block, (
            "the save must spread the existing categories, not overwrite them")


class TestStoryExtra:
    """The story package now carries options and publication links."""

    def _story(self, **kw):
        class S:
            name = "Sample Story"
            platform_options = kw.get("platform_options", {})
        return S()

    def test_platform_options_reach_the_package(self, monkeypatch):
        from posting import story_reader
        monkeypatch.setattr("database.posting_queries.get_publications",
                            lambda *a, **k: [])
        extra = story_reader._story_extra(
            self._story(platform_options={"tg": {"spoiler": True}}), "tg", 0)
        assert extra.get("spoiler") is True

    def test_links_are_populated_from_publications(self, monkeypatch):
        """The bug: nothing filled this, so announcements went out link-less."""
        from posting import story_reader
        monkeypatch.setattr("database.posting_queries.get_publications",
                            lambda *a, **k: [
                                {"external_url": "https://ao3.example/1",
                                 "platform": "ao3", "chapter_index": 0},
                                {"external_url": "https://sf.example/2",
                                 "platform": "sf", "chapter_index": 0},
                            ])
        extra = story_reader._story_extra(self._story(), "tg", 0)
        assert extra["links"] == ["https://ao3.example/1", "https://sf.example/2"]

    def test_the_target_platform_is_not_linked_to_itself(self, monkeypatch):
        """Don't tell the channel to go read the channel."""
        from posting import story_reader
        monkeypatch.setattr("database.posting_queries.get_publications",
                            lambda *a, **k: [
                                {"external_url": "https://t.me/c/1/2",
                                 "platform": "tg", "chapter_index": 0},
                                {"external_url": "https://ao3.example/1",
                                 "platform": "ao3", "chapter_index": 0},
                            ])
        extra = story_reader._story_extra(self._story(), "tg", 0)
        assert extra["links"] == ["https://ao3.example/1"]

    def test_duplicate_urls_collapse(self, monkeypatch):
        from posting import story_reader
        monkeypatch.setattr("database.posting_queries.get_publications",
                            lambda *a, **k: [
                                {"external_url": "https://x.example/1",
                                 "platform": "ao3", "chapter_index": 0},
                                {"external_url": "https://x.example/1",
                                 "platform": "sqw", "chapter_index": 0},
                            ])
        extra = story_reader._story_extra(self._story(), "tg", 0)
        assert extra["links"] == ["https://x.example/1"]

    def test_a_database_failure_degrades_to_a_linkless_post(self, monkeypatch):
        """An announcement with no links is still a valid 'this exists' post.
        A DB hiccup must not block a publish."""
        from posting import story_reader

        def boom(*a, **k):
            raise RuntimeError("db gone")

        monkeypatch.setattr("database.posting_queries.get_publications", boom)
        extra = story_reader._story_extra(self._story(), "tg", 0)
        assert "links" not in extra

    def test_the_poster_renders_the_links(self):
        from posting.platforms.base import StoryUploadPackage
        from posting.platforms.telegram import _build_caption
        pkg = StoryUploadPackage(
            story_name="Sample Story", chapter_index=1, chapter_title="Ch1",
            platform="tg", title="Sample Story", description="Chapter one is up.",
            tags=[], file_path=None, file_type="",
            extra={"links": ["https://ao3.example/1"]})
        out = _build_caption(pkg, has_image=False, is_art=False)
        assert "https://ao3.example/1" in out
