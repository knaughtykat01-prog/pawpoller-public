"""The story page on the C2 board (4.5.0) — spec §14, phase 2 items 13–19.

Source-string contracts on story_board.js, app.js, posting.js and bookshelf.js:
the board emits the update actions app.js dispatches (and never "upload-to"),
the canonical save carries expected_mtime and merges into the loaded record,
the old page's route redirects, the two read-only surfaces are gone, and
Publish Check is opened with one argument — nobody has quietly added a
container it does not take.
"""
from __future__ import annotations

import re

import pytest

SB = open("frontend/js/story_board.js", encoding="utf-8").read()
APP = open("frontend/js/app.js", encoding="utf-8").read()
POSTING = open("frontend/js/posting.js", encoding="utf-8").read()
SHELF = open("frontend/js/bookshelf.js", encoding="utf-8").read()
ART = open("frontend/js/artwork.js", encoding="utf-8").read()
HTML = open("frontend/index.html", encoding="utf-8").read()
API = open("frontend/js/api.js", encoding="utf-8").read()
CSS = open("frontend/css/board.css", encoding="utf-8").read()


def _fn(name: str, size: int = 9000) -> str:
    for opener in (f"        {name}(", f"        async {name}("):
        if opener in SB:
            i = SB.index(opener)
            return SB[i:i + size]
    raise AssertionError(f"{name} is not defined in story_board.js")


class TestActions:
    def test_update_single_is_emitted_with_the_dataset_app_js_reads(self):
        block = _fn("_locationsHtml")
        assert 'data-post-action="update-single"' in block
        for attr in ("data-post-story=", "data-post-platform=", "data-post-chapter="):
            assert attr in block
        # app.js:165 reads exactly these three
        assert "Posting._updateSingle(d.postStory, d.postPlatform, Number(d.postChapter))" in APP

    def test_update_all_is_emitted(self):
        assert 'data-post-action="update-all"' in _fn("_heroHtml")

    def test_the_board_never_emits_upload_to(self):
        assert "upload-to" not in SB

    def test_the_pushers_re_render_the_board_not_the_deleted_page(self):
        for fn in ("_updateSingle", "_updateAll"):
            i = POSTING.index(f"    async {fn}(")
            block = POSTING[i:i + 700]
            assert "StoryBoard.render(storyName)" in block, fn
            assert "renderStoryDetail" not in block, fn


class TestCanonicalSave:
    def test_sends_expected_mtime(self):
        block = _fn("_save")
        assert "expected_mtime: meta.last_modified" in block

    def test_merges_into_the_loaded_record_never_replaces_it(self):
        """The PUT writes the whole story.json; a five-key object would delete
        every field the drawer owns (§5.7)."""
        block = _fn("_save")
        assert "const next = { ...meta.metadata };" in block
        assert "next.tags = { ...(" in block, "per-site tag lists must survive a default-list edit"

    def test_a_409_is_a_reload_not_an_overwrite(self):
        block = _fn("_save")
        assert "/409/.test(" in block and "data-sb-reload" in block
        assert "merge" not in block.split("/409/")[1][:600].lower() or "Never auto-merge" in block

    def test_rating_is_a_select_over_the_values_the_put_accepts(self):
        assert "const RATINGS = ['Not Rated', 'General Audiences', 'Teen And Up Audiences', 'Mature', 'Explicit'];" in SB
        assert 'id="sb-e-rating"' in SB

    def test_the_api_methods_it_calls_exist(self):
        for m in ("getPostingStory", "getStoryMetadata", "saveStoryMetadata", "getStoryTagPreview", "linkStoryByUrl"):
            assert re.search(rf"^\s+{m}\(", API, re.M), f"API.{m} is not defined"
            assert f"API.{m}(" in SB


class TestPublishCheck:
    def test_is_opened_with_one_argument(self):
        """publish_check.js builds a modal with global singleton ids (§9 Q3);
        a container argument would mean someone mounted it in a card."""
        assert "PublishCheck.open(this._name)" in SB
        assert not re.search(r"PublishCheck\.open\([^)]*,", SB)

    def test_the_card_lists_unpublished_sites(self):
        assert "d.unpublished_platforms" in _fn("_publishHtml")


class TestLinkByUrl:
    def test_previews_then_confirms_with_an_explicit_chapter(self):
        pre = _fn("_linkPreviewRun")
        assert "API.linkStoryByUrl(this._name, { url })" in pre
        con = _fn("_linkConfirm")
        assert "chapter_index: Number(ch), confirm: true" in con
        assert "if (ch === '')" in con, "a multi-chapter story must make the user pick"

    def test_a_multi_chapter_story_offers_a_picker_and_a_single_one_does_not(self):
        pre = _fn("_linkPreviewRun")
        assert "total > 1" in pre and 'id="sb-link-chapter"' in pre


class TestRoutes:
    def test_library_work_goes_to_the_board(self):
        assert "StoryBoard.render(parts.slice(2).join('/'))" in APP

    def test_the_old_story_route_redirects(self):
        i = APP.index("parts[0] === 'posting' && parts[1] === 'story' && parts[2]")
        block = APP[i:i + 400]
        assert "location.replace('#/library/work/'" in block
        assert "renderStoryDetail" not in block

    def test_the_script_loads_after_its_dependencies(self):
        a = HTML.index("/js/posting.js")
        b = HTML.index("/js/publish_check.js")
        c = HTML.index("/js/tag_picker.js")
        d = HTML.index("/js/story_board.js")
        assert max(a, b, c) < d


class TestDeletions:
    def test_the_tabbed_story_page_is_gone(self):
        assert "renderStoryDetail(" not in POSTING
        assert "_renderComparisonChart(" in POSTING, "the chart renderer stays — the board calls it"
        assert "_updateSingle(" in POSTING and "_updateAll(" in POSTING

    def test_the_library_work_page_is_gone_but_the_grid_helpers_stay(self):
        assert "_paintWork(" not in SHELF and "renderWork(" not in SHELF
        for keep in ("_views(", "_faves(", "_comments(", "_num("):
            assert keep in SHELF, keep

    def test_the_dead_artwork_renderer_is_gone(self):
        assert "_renderDetailLegacy" not in ART


class TestBoardShape:
    def test_every_card_is_a_section_with_a_heading(self):
        for sec in ("sb-sec-canon", "sb-sec-tags", "sb-sec-budget", "sb-sec-pub", "sb-sec-link",
                    "sb-sec-loc", "sb-sec-growth", "sb-sec-needs"):
            assert f'<h2 id="{sec}">' in SB, sec

    def test_external_urls_go_through_safe_url(self):
        block = _fn("_locationsHtml")
        assert "Utils.safeUrl(b.url)" in block and "Utils.safeUrl(r.external_url)" in block

    def test_the_health_dot_treats_no_entry_as_not_checked(self):
        assert "PH.get(code) ? PH.classify(code) : 'unknown'" in _fn("_healthDot")

    def test_tags_are_a_view_over_the_hidden_textarea(self):
        assert '<textarea id="sb-e-tags"' in SB
        assert "getElementById('sb-e-tags')" in _fn("_tagsList")

    def test_needs_attention_has_an_empty_state(self):
        assert "Nothing needs you." in _fn("_attentionHtml")

    def test_board_css_styles_the_story_cards(self):
        for cls in (".sb-cover", ".pub-sub", ".needs-row", ".link-cand", ".sb-link"):
            assert cls in CSS, cls
