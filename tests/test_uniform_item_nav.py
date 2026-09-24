"""UNIFORMITEM — the item page's shared prev/next back-bar.

⚠ Read this before reopening the spec. `docs/specs/uniform_item_surface.md` was
captured 2026-08-27 and its Phase 1 ("fold `#/library/work/{name}` and
`#/posting/story/{name}` into one renderer") **was delivered in 4.5.0 by C2REDESIGN**,
six weeks later, under a different backlog row. The spec was never updated, so its
Phase 1 reads as outstanding when the page merge has shipped.

What had NOT shipped was the contract's *fixed navigation* (§2): prev/next over the
list you arrived from, with a position counter and arrow-key stepping. That existed
on masterpieces and nowhere else (§1.3), so every other item type was a dead end —
back to the grid, find your place, click the next one.

These tests pin both halves: that the merge is still in place, and that the nav is
now shared rather than copied.
"""
from __future__ import annotations

import re


def _read(path: str) -> str:
    return open(path, encoding="utf-8").read()


class TestThePageMergeIsStillInPlace:
    """Phase 1's actual deliverable, shipped 4.5.0. Pinned because nothing else
    pins it and the spec still describes the pre-merge world."""

    def test_the_posting_story_route_redirects_to_the_library_route(self):
        src = _read("frontend/js/app.js")
        i = src.index("parts[0] === 'posting' && parts[1] === 'story'")
        block = src[i:i + 600]
        assert "window.location.replace('#/library/work/'" in block, \
            "old links must still land somewhere real"

    def test_there_is_one_story_renderer_not_two(self):
        """The two deleted renderers are named in the comments left where they were,
        so a future reader does not go looking for them."""
        assert "renderStoryDetail (the tabbed story page) was deleted in 4.5.0" \
            in _read("frontend/js/posting.js")
        assert "was deleted in\n     * 4.5.0" in _read("frontend/js/bookshelf.js").replace(
            "\r\n", "\n")

    def test_the_story_board_carries_what_both_old_pages_had(self):
        """The superset the spec asked for: the posting page's record and publish
        controls, plus the library page's chapter reach and achievements."""
        src = _read("frontend/js/story_board.js")
        for needed in ("_canonicalHtml", "_chaptersHtml", "_publishHtml",
                       "_locationsHtml", "_growthHtml", "_laurelsHtml"):
            assert needed in src, f"the merged board lost {needed}"

    def test_it_uses_the_contracts_vocabulary(self):
        """§2 fixed vocabulary: section 2 is 'Published to' everywhere — not
        'Platforms', not 'Locations'."""
        src = _read("frontend/js/story_board.js")
        assert ">Published to</h2>" in src
        assert ">Growth</h2>" in src
        assert ">Locations</h2>" not in src


class TestThePrevNextNavIsSharedNotCopied:

    def test_the_shared_module_exists_and_is_loaded(self):
        src = _read("frontend/index.html")
        assert "/js/item_nav.js" in src
        # Before its consumers, so neither can race it.
        assert src.index("/js/item_nav.js") < src.index("/js/masterpieces.js")
        assert src.index("/js/item_nav.js") < src.index("/js/story_board.js")

    def test_masterpieces_delegates_rather_than_keeping_its_own(self):
        src = _read("frontend/js/masterpieces.js")
        assert "ItemNav.mount({" in src
        # The rendering it used to do itself is gone -- two implementations of one
        # back-bar is exactly what this work removes.
        assert "insertAdjacentHTML" not in src[src.index("_renderDetailNav"):
                                                src.index("_renderDetailNav") + 1200]

    def test_the_story_board_mounts_it(self):
        src = _read("frontend/js/story_board.js")
        assert "ItemNav.mount({" in src
        assert "_renderDetailNav(name)" in src

    def test_each_caller_supplies_only_what_is_its_own(self):
        """The split the generalisation makes: the module owns rendering and keys,
        the caller owns which list and where a name points."""
        nav = _read("frontend/js/item_nav.js")
        assert "#/masterpieces/" not in nav, "the module must not know its callers"
        assert "#/library/work/" not in nav.split("Usage:")[1].split("*/")[1], \
            "only the usage example may name a route"

    def test_the_callers_keep_their_own_list_rules(self):
        mp = _read("frontend/js/masterpieces.js")
        assert "status !== 'junk'" in mp, "junk filtering is masterpiece-shaped"
        sb = _read("frontend/js/story_board.js")
        assert "content_type === 'story'" in sb


class TestTheNavCannotLie:

    def test_it_renders_nothing_when_it_cannot_place_the_item(self):
        """An absent counter is honest; arrows that cannot step are not."""
        nav = _read("frontend/js/item_nav.js")
        # the DEFINITION, not the usage example in the docstring above it
        i = nav.index("mount({ names, current")
        block = nav[i:i + 700]
        assert "indexOf(current)" in block
        assert "if (idx === -1) return false" in block

    def test_a_deep_link_with_no_cache_still_gets_a_list(self):
        """Arriving from search or the Publish Check matrix has no cached order."""
        sb = _read("frontend/js/story_board.js")
        i = sb.index("_renderDetailNav")
        block = sb[i:i + 1200]
        assert "Bookshelf._works" in block
        assert "API.getWorks()" in block, "must fall back to fetching"

    def test_a_failed_fetch_leaves_the_plain_back_link(self):
        sb = _read("frontend/js/story_board.js")
        i = sb.index("_renderDetailNav")
        assert "catch (e) { return; }" in sb[i:i + 1200]

    def test_a_repaint_does_not_stack_a_second_set_of_arrows(self):
        nav = _read("frontend/js/item_nav.js")
        assert "back.querySelector('.item-nav')" in nav

    def test_arrow_keys_do_not_fire_while_typing(self):
        nav = _read("frontend/js/item_nav.js")
        i = nav.index("onKey(e)")
        block = nav[i:i + 900]
        assert "input, textarea, select, [contenteditable]" in block

    def test_arrow_keys_stop_when_the_page_changes(self):
        """A hash change does not clear state, so without the route test the arrows
        would keep firing on whatever page came next."""
        nav = _read("frontend/js/item_nav.js")
        i = nav.index("onKey(e)")
        assert "_routeTest(location.hash" in nav[i:i + 900]

    def test_both_masterpiece_routes_still_step(self):
        """2.193.0 made two routes render that page; the route test has to cover both
        or arrow-stepping silently dies on one of them."""
        mp = _read("frontend/js/masterpieces.js")
        i = mp.index("ItemNav.mount({")
        block = mp[i:i + 500]
        assert "masterpieces" in block and "artwork" in block

    def test_the_href_is_escaped(self):
        nav = _read("frontend/js/item_nav.js")
        assert 'href="${esc(href(n))}"' in nav


class TestTheStylingMovedWithIt:

    def test_it_lives_beside_the_back_bar_it_extends(self):
        css = _read("frontend/css/bookshelf.css")
        assert ".work-back.item-detail-topnav" in css
        assert ".item-nav {" in css

    def test_the_masterpiece_only_rules_are_gone(self):
        """They were scoped to one page for as long as one page had the nav."""
        css = _read("frontend/css/masterpieces.css")
        assert ".work-back.mp-detail-topnav" not in css
        assert ".mp-nav {" not in css

    def test_nothing_still_emits_the_retired_class_names(self):
        for p in ("frontend/js/item_nav.js", "frontend/js/masterpieces.js",
                  "frontend/js/story_board.js"):
            src = _read(p)
            assert "mp-nav" not in src, f"{p} still emits a retired class"
            assert "mp-detail-topnav" not in src

    def test_the_stylesheet_is_globally_loaded(self):
        """The nav appears on pages that are not the bookshelf, so a lazily-loaded
        stylesheet would leave it unstyled."""
        html = _read("frontend/index.html")
        assert re.search(r'href="/css/bookshelf\.css', html)
