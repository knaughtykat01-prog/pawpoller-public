"""The Masterpiece page on the C2 board (4.4.0) — spec §14, phase 1.

Source-string tests, the same contract style as test_publish_confirm.py. They
are cheap, and they are the only thing standing between a refactor of this
size and a silently broken SFW mode: the blur selectors in app.js and
safe_mode.css key on `img.mp-hero-img[data-rating]` and
`.mp-alts[data-rating]`, and a page that renamed either would leak adult
renders unblurred without a single error.
"""
from __future__ import annotations

import re

import pytest

JS = open("frontend/js/masterpieces.js", encoding="utf-8").read()
CSS = open("frontend/css/board.css", encoding="utf-8").read()
MPCSS = open("frontend/css/masterpieces.css", encoding="utf-8").read()
HTML = open("frontend/index.html", encoding="utf-8").read()
APP = open("frontend/js/app.js", encoding="utf-8").read()
SFW = open("frontend/css/safe_mode.css", encoding="utf-8").read()


def _fn(name: str, size: int = 12000) -> str:
    """The source of one method, sync or async."""
    for opener in (f"    {name}(", f"    async {name}("):
        if opener in JS:
            i = JS.index(opener)
            return JS[i:i + size]
    raise AssertionError(f"{name} is not defined in masterpieces.js")


def _rules(css: str) -> str:
    """CSS with its comments stripped — a rule, not a mention of one."""
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


# ── Layout (§14 items 1–4) ───────────────────────────────────────────────────

class TestLayout:
    def test_board_css_is_loaded_after_masterpieces_and_before_safe_mode(self):
        a = HTML.index("/css/masterpieces.css")
        b = HTML.index("/css/board.css")
        c = HTML.index("/css/safe_mode.css")
        assert a < b < c, "safe_mode.css must stay last — it wins the SFW cascade"

    def test_the_three_breakpoints_exist(self):
        """No media query, and the 2560px complaint returns."""
        assert "@media (max-width: 1400px)" in CSS
        assert "@media (max-width: 900px)" in CSS
        assert "grid-template-columns: repeat(3, minmax(0, 1fr))" in CSS

    def test_column_three_spans_at_two_columns(self):
        i = CSS.index("@media (max-width: 1400px)")
        assert ".board-col--3 { grid-column: 1 / -1; }" in CSS[i:i + 400]

    def test_the_retired_rules_are_deleted_not_overridden(self):
        """Two answers to one question is how the next reader gets it wrong."""
        rules = _rules(MPCSS)
        assert ".mp-edit { max-width" not in rules
        assert "max-width: 640px" not in rules
        assert "max-width: 46ch" not in rules
        assert ".mp-detail-head {" not in rules
        assert ".mp-headline" not in rules, "the headline row is the hero's .board-stats now"

    def test_the_board_uses_live_tokens_only(self):
        """Spec §12: the mockup's two drifted tokens are not reproduced, and
        no colour is a literal — the board must retone with the app."""
        rules = _rules(CSS)
        assert "0.12s" not in rules, "the mockup's stale --transition"
        assert not re.search(r"#(?:[0-9a-fA-F]{3}){1,2}\b", rules), "a literal colour in board.css"
        assert "0 4px 20px rgba(0, 0, 0, 0.25)" not in rules, "the mockup's stale --shadow-glass"


# ── The SFW contract (§4, §14 items 5–7) ─────────────────────────────────────

class TestSfwContract:
    def test_the_hero_keeps_its_id_class_and_rating(self):
        assert 'class="mp-hero-img" id="mp-hero-img" data-rating="${v.rating}"' in JS

    def test_the_variant_strip_keeps_its_class_and_rating(self):
        assert '<div class="mp-alts" data-rating="${v.rating}">' in JS

    def test_the_selectors_are_unchanged_in_both_places(self):
        for src, where in ((APP, "app.js"), (SFW, "safe_mode.css")):
            assert 'img.mp-hero-img:not([data-rating="general"])' in src, where
            assert '.mp-alts:not([data-rating="general"]) img' in src, where

    def test_the_lightbox_is_blurred_too(self):
        """A full-size adult render opened in SFW mode must not leak."""
        block = _fn("_openLightbox")
        assert 'class="mp-hero-img" data-rating="${this.esc(img.dataset.rating' in block


# ── Ids other modules reach for (§2, §14 items 8–10) ─────────────────────────

IDS = [
    "mp-hero-img", "mp-stage-bg", "mp-replace-file", "mp-addvariant-file", "mp-artist-body",
    "mp-e-title", "mp-e-desc", "mp-e-alt", "mp-e-rating", "mp-e-chars", "mp-e-tags",
    "mp-edit-msg", "mp-tagbudget", "mp-detail-platforms", "mp-pub-msg", "mp-schedule-form",
    "mp-schedule-datetime", "mp-scheduled-list", "mp-suggest-body", "mp-fold-chosen",
    "mp-fold-vlabel-wrap", "mp-fold-vlabel", "mp-fold-msg", "mp-chart-card", "mp-combined-chart",
    "mp-vstats", "mp-replace-msg",
]
ACTIONS = [
    "data-mp-save", "data-mp-sync", "data-mp-tagbrowse", "data-mp-scan", "data-mp-linkpick",
    "data-mp-linkurl", "data-mp-detach", "data-mp-junk", "data-mp-publish", "data-mp-schedule-toggle",
    "data-mp-schedule-cancel", "data-mp-schedule-confirm", "data-mp-delete", "data-mp-artist-edit",
    "data-mp-fold-pick", "data-mp-fold", "data-mp-vrename", "data-mp-vsplit", "data-mp-isplit",
    "data-mp-img", "data-add-collection",
]


class TestIdsSurvive:
    @pytest.mark.parametrize("id_", IDS)
    def test_id_is_emitted(self, id_):
        assert f'id="{id_}"' in JS, f"#{id_} is read by a handler or a fill and is gone"

    @pytest.mark.parametrize("action", ACTIONS)
    def test_action_is_emitted(self, action):
        assert re.search(rf"\s{re.escape(action)}(\s|=|>)", JS), f"{action} has no element to fire from"

    def test_read_canonical_still_finds_its_six_inputs(self):
        block = _fn("_readCanonical", 900)
        for id_ in ("mp-e-title", "mp-e-desc", "mp-e-rating", "mp-e-chars", "mp-e-tags", "mp-e-alt"):
            assert f"'{id_}'" in block

    def test_the_tags_textarea_survives_the_chip_ui(self):
        """The chips are a VIEW over #mp-e-tags (§5.3). A test that only
        checked for chips would pass on a rewrite that broke saving."""
        assert '<textarea id="mp-e-tags"' in JS
        assert "getElementById('mp-e-tags')" in _fn("_tagChips")


# ── Composition (§14 items 11–12) ────────────────────────────────────────────

RENDERERS = ["_heroHtml", "_canonicalHtml", "_tagsHtml", "_budgetHtml", "_publishHtml",
             "_linkHtml", "_foldHtml", "_locationsHtml", "_growthHtml", "_bestHtml", "_rendersHtml"]


class TestComposition:
    @pytest.mark.parametrize("name", RENDERERS)
    def test_each_renderer_is_called_exactly_once_and_defined(self, name):
        body = _fn("_paintDetail", 2600)
        assert body.count(f"this.{name}(") == 1, name
        assert f"    {name}(" in JS, f"{name} is not defined"

    def test_paint_detail_is_an_assembler_not_a_template(self):
        body = _fn("_paintDetail", 2600)
        i = body.index("root.innerHTML")
        j = body.index("`;", i)
        assert j - i < 900, "the assembler grew a template of its own"

    def test_the_post_paint_fills_are_unchanged(self):
        body = _fn("_paintDetail", 2600)
        for call in ("this._wireDetailPublish(name, m)", "this._loadSuggestions()",
                     "this._loadTagBudget()", "this._loadChart(name)"):
            assert call in body

    def test_external_urls_go_through_safe_url(self):
        block = _fn("_locationsHtml")
        assert "Utils.safeUrl(l.url)" in block
        assert 'href="${this.esc(safe)}"' in block

    def test_best_performer_needs_no_api(self):
        block = _fn("_bestHtml")
        assert "API." not in block and "locations" in block

    def test_the_health_dot_reads_the_cache_not_the_network(self):
        block = _fn("_healthDot")
        assert "PH.classify(code)" in block and "fetch(" not in block and "API." not in block
        assert "PH.get(code) ? PH.classify(code) : 'unknown'" in block, (
            "no entry means not checked, not unconfigured — a dot must not assert a fault it has not seen")


# ── Tag chips (§5.3, §13) ────────────────────────────────────────────────────

class TestChips:
    def test_remove_is_a_real_button_with_a_label(self):
        block = _fn("_tagChips")
        assert '<button type="button" class="x" data-mp-chip-x=' in block
        assert 'aria-label="Remove tag ${this.esc(t)}"' in block

    def test_core_and_cut_come_from_the_tag_preview(self):
        block = _fn("_tagChips")
        assert "d.core_count" in block and "p.dropped" in block
        assert "tagchip--core" in block and "tagchip--cut" in block

    def test_without_a_preview_the_chips_are_still_drawn(self):
        """A failed tag-preview must not blank the tags (§11)."""
        block = _fn("_tagChips")
        assert "const d = this._budget || null;" in block

    def test_focus_moves_after_a_removal(self):
        block = _fn("_removeTag")
        assert "next.focus()" in block and "[data-mp-chip-add]" in block

    def test_the_add_box_commits_on_enter_and_cancels_on_escape(self):
        i = JS.index("e.target.id === 'mp-tag-add'")
        block = JS[i:i + 1200]
        assert "e.key === 'Enter'" in block and "e.key === 'Escape'" in block and "e.key === 'Backspace'" in block

    def test_browse_writes_the_textarea_and_rerenders(self):
        block = _fn("_openTagBrowse")
        assert "this._tagChips()" in block

    def test_the_budget_load_styles_the_chips(self):
        block = _fn("_loadTagBudget", 3000)
        assert "this._tagChips();" in block
        assert "data-mp-budget-retry" in block, "a failed load offers a retry, not a blank"

    def test_the_browse_button_is_visible_now(self):
        assert 'class="btn btn-sm btn-browse" data-mp-tagbrowse' in JS


# ── States (§11) and keyboard (§13) ─────────────────────────────────────────

class TestStates:
    def test_a_junked_piece_keeps_every_card_but_cannot_publish(self):
        block = _fn("_publishHtml")
        assert "m.status === 'junk'" in block and "Restore this piece to publish it." in block

    def test_the_hero_tile_is_a_button(self):
        assert '<button type="button" class="board-hero-tile" data-mp-lightbox' in JS

    def test_cards_are_sections_with_headings(self):
        for sec in ("mp-sec-canon", "mp-sec-tags", "mp-sec-budget", "mp-sec-pub", "mp-sec-link",
                    "mp-sec-fold", "mp-sec-loc", "mp-sec-growth", "mp-sec-best"):
            assert f'<h2 id="{sec}">' in JS, sec
        assert JS.count('<h1 class="mp-title">') == 1

    def test_secondary_cards_show_a_skeleton_while_loading(self):
        assert 'class="card-skel"' in _fn("_budgetHtml")
        assert ".card-skel" in CSS

    def test_the_renders_card_is_native_details_and_collapsed(self):
        block = _fn("_rendersHtml")
        assert '<details class="card renders-card">' in block and "<details open" not in block
