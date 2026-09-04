"""Telegram links and descriptions (4.3.0).

docs/specs/publish_flow.md §5, §6, §8.3, §8.4, §10 Q3/Q6. Three findings held
in place here:

* an artwork announcement never had a link to point at — nothing in the repo
  produced ``extra['links']`` for artwork — and a story's links came out in
  alphabetical order by platform code, which nobody chose;
* the link picker's two options are a mode and an ORDERED LIST, and must never
  pass through ``_flag()`` (a list coerced to True was the failure §6 named);
* "wherever it lands first" needs the announcer to post LAST, so its links can
  include what this same publish just created.
"""
from __future__ import annotations

import os
import tempfile

import pytest

from posting.platforms.telegram import _resolve_links, _build_caption
from posting.platforms.base import StoryUploadPackage


def _pkg(extra=None, **kw):
    base = dict(story_name="Sample_Story", title="Sample", chapter_index=0,
                chapter_title="", platform="tg", description="", tags=[], rating="general",
                file_path="", file_type="", word_count=0)
    base.update(kw)
    p = StoryUploadPackage(**base)
    p.extra = extra or {}
    return p


EXISTING = [("fa", "https://example.com/fa/1"), ("ib", "https://example.com/ib/2")]
RUN = [("ws", "https://example.com/ws/3"), ("sf", "https://example.com/sf/4")]


class TestResolveLinks:
    def test_auto_prefers_existing_links(self):
        assert _resolve_links(_pkg({"links_by_platform": EXISTING, "run_links": RUN})) == [
            "https://example.com/fa/1", "https://example.com/ib/2",
            "https://example.com/ws/3", "https://example.com/sf/4"]

    def test_auto_on_a_fresh_piece_is_wherever_it_lands_first(self):
        """§10 Q3: nothing live yet → the first link THIS publish produced."""
        assert _resolve_links(_pkg({"run_links": RUN})) == ["https://example.com/ws/3"]

    def test_auto_with_nothing_at_all_is_empty(self):
        assert _resolve_links(_pkg({})) == []

    def test_first_takes_this_run_before_existing(self):
        assert _resolve_links(_pkg({"link_mode": "first", "links_by_platform": EXISTING,
                                    "run_links": RUN})) == ["https://example.com/ws/3"]
        assert _resolve_links(_pkg({"link_mode": "first", "links_by_platform": EXISTING})) == [
            "https://example.com/fa/1"]

    def test_pick_filters_and_orders_by_the_users_list(self):
        """The alphabetical accident (§6) is gone: the user's order wins."""
        assert _resolve_links(_pkg({"link_mode": "pick", "link_platforms": ["ib", "sf"],
                                    "links_by_platform": EXISTING, "run_links": RUN})) == [
            "https://example.com/ib/2", "https://example.com/sf/4"]

    def test_all_orders_listed_first_then_the_rest(self):
        assert _resolve_links(_pkg({"link_mode": "all", "link_platforms": ["sf"],
                                    "links_by_platform": EXISTING, "run_links": RUN})) == [
            "https://example.com/sf/4", "https://example.com/fa/1",
            "https://example.com/ib/2", "https://example.com/ws/3"]

    def test_none_is_none_even_with_links(self):
        assert _resolve_links(_pkg({"link_mode": "none", "links_by_platform": EXISTING})) == []

    def test_the_list_is_read_raw_not_as_a_flag(self):
        """A list must stay a list. `_flag(["ao3","fa"])` is True; this must not be."""
        out = _resolve_links(_pkg({"link_mode": "pick", "link_platforms": ["fa"],
                                   "links_by_platform": EXISTING}))
        assert out == ["https://example.com/fa/1"]

    def test_bare_url_list_still_works(self):
        """Older packages carry only extra['links']."""
        assert _resolve_links(_pkg({"links": ["https://example.com/x"]})) == ["https://example.com/x"]

    def test_unknown_mode_falls_back_to_auto_and_urls_dedupe(self):
        dup = EXISTING + [("ws", "https://example.com/fa/1")]
        assert _resolve_links(_pkg({"link_mode": "bogus", "links_by_platform": dup})) == [
            "https://example.com/fa/1", "https://example.com/ib/2"]


class TestCaption:
    def test_artwork_caption_now_carries_links(self):
        """§6: artwork got no links at all before 4.3.0."""
        p = _pkg({"links_by_platform": EXISTING}, description="A piece", file_path="x.png", file_type="png")
        cap = _build_caption(p, has_image=True, is_art=True, with_tags=False)
        assert "A piece" in cap and "https://example.com/fa/1" in cap and "https://example.com/ib/2" in cap

    def test_story_caption_respects_the_mode(self):
        p = _pkg({"link_mode": "none", "links_by_platform": EXISTING}, description="blurb")
        cap = _build_caption(p, has_image=False, is_art=False, with_tags=False)
        assert "https://" not in cap and "blurb" in cap


class TestManagerOrdering:
    def test_announcers_post_last_and_the_rest_keep_their_order(self):
        from posting.manager import _announcers_last
        assert _announcers_last(["tg", "fa", "ib", "ws"]) == ["fa", "ib", "ws", "tg"]
        assert _announcers_last(["fa"]) == ["fa"]

    def test_run_links_take_successes_with_urls_only(self):
        from posting.manager import _run_links
        results = [
            {"platform": "fa", "success": True, "external_url": "https://example.com/fa/1"},
            {"platform": "ib", "success": False, "external_url": "https://example.com/ib/x"},
            {"platform": "ws", "success": True, "external_url": ""},
            {"platform": "sf", "success": True, "url": "https://example.com/sf/2"},
        ]
        assert _run_links(results) == [("fa", "https://example.com/fa/1"), ("sf", "https://example.com/sf/2")]

    def test_run_links_are_chapter_aware_for_stories(self):
        from posting.manager import _run_links
        results = [
            {"platform": "ao3", "success": True, "chapter_index": 1, "external_url": "https://example.com/1"},
            {"platform": "sf", "success": True, "chapter_index": 2, "external_url": "https://example.com/2"},
            {"platform": "ib", "success": True, "chapter_index": 0, "external_url": "https://example.com/0"},
        ]
        assert _run_links(results, 2) == [("sf", "https://example.com/2"), ("ib", "https://example.com/0")]

    def test_both_loops_iterate_announcers_last_and_forward_overrides(self):
        src = open("posting/manager.py", encoding="utf-8").read()
        assert src.count("for platform in _announcers_last(platforms):") == 2
        assert src.count('package.extra["run_links"] = _run_links(results') == 2
        assert src.count("description_override=(description_overrides or {}).get(platform)") == 2


class TestArtworkLinks:
    @pytest.fixture()
    def conn(self, monkeypatch):
        import config
        from database import db as dbm
        monkeypatch.setattr(config, "DB_PATH", os.path.join(tempfile.mkdtemp(), "tg.db"))
        dbm.init_db()
        yield

    def test_reads_artwork_publications_not_stories(self, conn):
        """content_type='artwork' — the default 'story' filter would find nothing."""
        from posting import artwork_reader
        from database import posting_queries
        from database.db import get_connection
        c = get_connection()
        try:
            posting_queries.upsert_publication(
                c, "Sample_Piece", 0, "fa", content_type="artwork",
                external_id="1", external_url="https://example.com/fa/1", status="posted")
            posting_queries.upsert_publication(
                c, "Sample_Piece", 0, "tg", content_type="artwork",
                external_id="9", external_url="https://example.com/tg/9", status="posted")
            posting_queries.upsert_publication(
                c, "Sample_Piece", 0, "ib", content_type="artwork",
                external_id="2", external_url="https://example.com/ib/2", status="failed")
            c.commit()
        finally:
            c.close()
        got = artwork_reader._artwork_links("Sample_Piece", exclude="tg")
        assert got == {"links": ["https://example.com/fa/1"],
                       "links_by_platform": [("fa", "https://example.com/fa/1")]}, (
            "posted only, the channel's own link excluded")


class TestPlumbing:
    """The per-post override reaches a poster from every entry point."""

    def _src(self, p):
        return open(p, encoding="utf-8").read()

    def test_routes_accept_and_forward(self):
        assert 'description_overrides = body.get("description_overrides")' in self._src("routes/artwork_api.py")
        assert "description_override=description_override" in self._src("routes/artwork_api.py"), "schedule → queue row"
        assert 'description_overrides = body.get("description_overrides")' in self._src("routes/posting_api.py")
        assert "description_override: str | None = None" in self._src("routes/editor_api.py")

    def test_scheduler_finally_forwards_the_queue_column(self):
        """posting_queries persisted description_override all along; nothing read it (§10 Q6)."""
        src = self._src("posting/scheduler.py")
        assert 'item["description_override"]' in src
        assert src.count("description_overrides=description_overrides") == 2

    def test_story_links_carry_platform_codes(self):
        src = self._src("posting/story_reader.py")
        assert 'extra["links_by_platform"] = pairs' in src

    def test_frontend_surfaces(self):
        comp = self._src("frontend/js/components.js")
        assert "data-pub-tgdesc" in comp and "tgDescription" in comp
        art = self._src("frontend/js/artwork.js")
        assert "art-tg-linkmode" in art and "art-tg-desc" in art
        assert "out.link_mode = modeEl.value" in art and "out.link_platforms = picks" in art
        assert "updates.descriptions = descriptions" in art, "the stored text is merged, not replaced"
        assert art.count("description_overrides: ") == 3, "new form, publish-more, quick publish"
        mp = self._src("frontend/js/masterpieces.js")
        assert "payload.descriptions = descriptions" in mp
        # 4.3.7: the dialog's text boxes are one per announcer (X and Bluesky
        # joined Telegram), so the masterpiece page hands the whole result to
        # Artwork._pubDescOverrides rather than lifting tgDescription alone.
        assert "description_overrides: window.Artwork ? window.Artwork._pubDescOverrides(conf)" in mp
        me = self._src("frontend/js/metadata_editor.js")
        assert 'data-desc-tab="tg"' in me and "Announcement (Bsky/Telegram)" in me
        assert "metadata-tg-linkmode" in me and "tg.link_platforms = picks" in me
        pc = self._src("frontend/js/publish_check.js")
        assert "publish-tg-desc" in pc and "description_override:" in pc
