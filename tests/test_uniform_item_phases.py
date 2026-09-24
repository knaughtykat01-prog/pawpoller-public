"""UNIFORMITEM phases 2–5 — the shared frame, the other item types, the post page.

Phase 1 shipped across 4.5.0 and 4.34.3 (see `test_uniform_item_nav.py`). This
covers the rest of `docs/specs/uniform_item_surface.md`:

* **2** — the hero shell, headline row and section head extracted to `item_frame.js`
* **3** — collections and commissions onto the frame, with the contract's vocabulary
* **4** — `#/posts/{id}`, the one phase that is a feature rather than a consolidation
* **5** — retire what is now unreachable
"""
from __future__ import annotations

import sqlite3

import pytest


def _read(path: str) -> str:
    return open(path, encoding="utf-8").read()


# ── Phase 2 ──────────────────────────────────────────────────────────────────

class TestTheFrameIsSharedNotCopied:

    def test_the_module_exists_and_loads_before_every_consumer(self):
        html = _read("frontend/index.html")
        i = html.index("/js/item_frame.js")
        for consumer in ("/js/posts.js", "/js/post_board.js", "/js/collections.js",
                         "/js/commissions.js", "/js/masterpieces.js", "/js/story_board.js"):
            assert i < html.index(consumer), f"{consumer} loads before the frame"

    def test_the_headline_row_has_one_implementation(self):
        """It had been hand-written three times, which is how the labels drifted."""
        for p in ("frontend/js/masterpieces.js", "frontend/js/story_board.js",
                  "frontend/js/post_board.js"):
            src = _read(p)
            assert "ItemFrame.statRow(" in src, f"{p} does not use the shared row"
            assert '<div class="board-stats">' not in src, f"{p} still builds its own"

    def test_the_back_bar_has_one_implementation(self):
        for p in ("frontend/js/masterpieces.js", "frontend/js/story_board.js",
                  "frontend/js/collections.js", "frontend/js/commissions.js",
                  "frontend/js/post_board.js"):
            src = _read(p)
            assert "ItemFrame.backBar(" in src, f"{p} hand-rolls its back-bar"

    def test_the_labels_no_longer_drift(self):
        """The story board said Reads/Faves where the masterpiece page said
        Views/Favourites — the same numbers under two names, on two pages the spec
        exists to make consistent."""
        sb = _read("frontend/js/story_board.js")
        assert "label: 'Views'" in sb and "label: 'Favourites'" in sb
        assert "'Reads'" not in sb and "label: 'Faves'" not in sb

    def test_null_is_not_rendered_as_zero(self):
        """'Not measured' and 'nobody looked' are different statements."""
        src = _read("frontend/js/item_frame.js")
        assert "'—'" in src
        i = src.index("const fmt =")
        assert "n == null" in src[i:i + 300]

    def test_an_empty_row_renders_nothing(self):
        """A type with no numbers omits the slot rather than showing an empty strip."""
        src = _read("frontend/js/item_frame.js")
        i = src.index("statRow(stats)")
        assert "cells ?" in src[i:i + 500]

    def test_the_section_vocabulary_is_exported(self):
        src = _read("frontend/js/item_frame.js")
        for word in ("'Record'", "'Published to'", "'Publish to more'", "'Growth'",
                     "'Related'", "'History'"):
            assert word in src, f"{word} missing from the shared vocabulary"

    def test_an_empty_section_is_omitted(self):
        src = _read("frontend/js/item_frame.js")
        i = src.index("section({")
        assert "if (!body) return ''" in src[i:i + 400]

    def test_the_retrofit_decision_is_written_down(self):
        """~30 existing .sec-title blocks were deliberately NOT rewritten; a reader
        should find out why here rather than assume it was missed."""
        src = _read("frontend/js/item_frame.js")
        assert "were NOT rewritten" in src


# ── Phase 3 ──────────────────────────────────────────────────────────────────

class TestCollectionsAndCommissionsUseTheContract:

    def test_locations_is_now_published_to(self):
        src = _read("frontend/js/collections.js")
        assert ">Published to</h3>" in src
        assert ">Locations</h3>" not in src

    def test_companion_story_is_now_related(self):
        src = _read("frontend/js/collections.js")
        assert ">Related</h3>" in src
        assert ">Companion story</h3>" not in src

    def test_combined_growth_is_now_growth(self):
        src = _read("frontend/js/collections.js")
        assert ">Growth</h3>" in src
        assert ">Combined growth</h3>" not in src

    def test_platforms_stat_is_now_sites(self):
        src = _read("frontend/js/collections.js")
        assert "statCard('Sites'" in src
        assert "statCard('Platforms'" not in src

    def test_both_get_prev_next(self):
        for p in ("frontend/js/collections.js", "frontend/js/commissions.js"):
            assert "ItemNav.mount({" in _read(p), f"{p} is still a dead end"

    def test_the_archived_board_is_not_mistaken_for_a_commission(self):
        """#/commissions/archived is a list route; arrow-stepping there would try to
        open a commission called 'archived'."""
        src = _read("frontend/js/commissions.js")
        i = src.index("ItemNav.mount({")
        assert "archived" in src[i:i + 600]

    def test_commissions_does_not_invent_a_stat_row(self):
        """A commission has a price and a due date, not views and favourites. The
        contract says omit the slot, never repurpose it."""
        src = _read("frontend/js/commissions.js")
        assert "ItemFrame.statRow(" not in src
        assert "No headline stat row here" in src


# ── Phase 4 ──────────────────────────────────────────────────────────────────

class TestThePostItemPage:
    """The one phase that is a feature. A post goes to up to six platforms and every
    one writes a publication row, but the feed was the whole surface."""

    def test_the_route_exists_and_is_not_swallowed_by_the_feed(self):
        src = _read("frontend/js/app.js")
        i = src.index("PostBoard.render(")
        # must be matched BEFORE the bare posts route, or the feed always wins
        assert i < src.index("window.Posts.render()")

    def test_new_and_contacts_are_still_pages_not_posts(self):
        src = _read("frontend/js/app.js")
        i = src.index("PostBoard.render(")
        before = src[:i]
        assert "parts[1] === 'new'" in before
        assert "parts[1] === 'contacts'" in before

    def test_arrow_stepping_skips_those_two(self):
        src = _read("frontend/js/post_board.js")
        i = src.index("routeTest:")
        assert "new|contacts" in src[i:i + 200]

    def test_the_feed_links_into_it(self):
        assert 'href="#/posts/${p.post_id}"' in _read("frontend/js/posts.js")

    def test_it_is_built_on_the_shared_frame(self):
        src = _read("frontend/js/post_board.js")
        for needed in ("ItemFrame.backBar(", "ItemFrame.hero(", "ItemFrame.statRow(",
                       "ItemFrame.SECTIONS", "ItemNav.mount("):
            assert needed in src, f"the post page rolls its own {needed}"

    def test_it_names_sections_from_the_shared_vocabulary(self):
        """So it cannot invent a synonym the way Collections had with 'Locations'."""
        src = _read("frontend/js/post_board.js")
        assert "SECTIONS.publishedTo" in src
        # below the header comment -- which names the heading while explaining why
        code = src[src.index("(function ()"):]
        assert '"Published to"' not in code and "'Published to'" not in code


class TestThePostDetailApi:

    @pytest.fixture
    def pubs(self):
        return [
            {"platform": "bsky", "status": "posted", "external_id": "1",
             "external_url": "https://e.example/1", "stats": {"views": 10, "favorites": 2, "comments": 1}},
            {"platform": "mast", "status": "posted", "external_id": "2",
             "external_url": "", "stats": {"views": None, "favorites": None, "comments": None}},
            {"platform": "tw", "status": "failed", "external_id": "",
             "external_url": "", "stats": {"views": None, "favorites": None, "comments": None}},
        ]

    def test_totals_count_only_posted_sites(self, pubs):
        from routes.posts_api import _post_totals
        assert _post_totals(pubs)["sites"] == 2

    def test_unmeasured_stats_do_not_count_as_zero(self, pubs):
        """Summing a None as 0 would make 'not polled yet' indistinguishable from
        'nobody engaged', which is the confusion this whole spec is about."""
        from routes.posts_api import _post_totals
        t = _post_totals(pubs)
        assert t["views"] == 10 and t["favorites"] == 2 and t["comments"] == 1

    def test_a_publication_is_never_dropped(self):
        """A posted row with no stored submission still has to appear -- 'posted but
        not measured' is a state the page must be able to show."""
        from routes.posts_api import _resolve_post_publications
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        rows = _resolve_post_publications(conn, [
            {"platform": "bsky", "status": "posted", "external_id": "999",
             "external_url": "https://e.example/9", "account_id": 1}])
        assert len(rows) == 1
        assert rows[0]["stats"] == {"views": None, "favorites": None, "comments": None}

    def test_no_publications_means_no_snapshot_query(self):
        """A draft has nothing to chart, and an empty IN() is not a question worth
        asking the database."""
        import inspect
        from routes import posts_api
        src = inspect.getsource(posts_api.get_post_snapshots)
        assert "if not pairs:" in src

    def test_the_growth_chart_reuses_the_collection_aggregator(self):
        import inspect
        from routes import posts_api
        src = inspect.getsource(posts_api.get_post_snapshots)
        assert "get_combined_snapshots" in src

    def test_the_publications_are_resolved_in_bulk(self):
        """One query per platform, not one per publication -- the batching that keeps
        the Masterpiece and Collection rollups from crawling."""
        import inspect
        from routes import posts_api
        src = inspect.getsource(posts_api._resolve_post_publications)
        assert "_submission_rows_bulk" in src


# ── Phase 5 ──────────────────────────────────────────────────────────────────

class TestWhatIsUnreachableIsGone:

    def test_the_orphaned_stat_helpers_are_deleted(self):
        """_pick/_views/_faves/_comments served _paintWork, retired in 4.5.0."""
        src = _read("frontend/js/bookshelf.js")
        assert "_pick(stats, keys)" not in src
        assert "_views(s) {" not in src
        assert "deleted in 4.34.4" in src, "say where they went"

    def test_the_hardcoded_telegram_wrappers_are_deleted(self):
        src = _read("frontend/js/artwork.js")
        assert "_collectTgDesc()" not in src
        assert "_collectTgOpts()" not in src
        # the generic ones they wrapped stay
        assert "_collectPlatDesc(code)" in src
        assert "_collectPlatOpts(code)" in src

    def test_story_links_point_at_the_canonical_route(self):
        """They all worked, via a redirect. Pointing them straight at the real page
        is what makes the redirect a compatibility shim rather than the design."""
        for p in ("frontend/js/posting.js", "frontend/js/collections.js"):
            assert "#/posting/story/" not in _read(p), f"{p} still takes the hop"

    def test_the_redirect_itself_stays(self):
        """Old bookmarks and anything outside the repo still have to land."""
        assert "#/library/work/" in _read("frontend/js/app.js")

    # The assertion that phase 5's deliberate non-deletion is logged as
    # ARTPHANTOMDOM lives in tests/test_public_copy.py, because it reads
    # docs/BACKLOG.md and the public copy strips that file by name. A test that
    # reads a private-only doc cannot run in the public checkout, and the public
    # CI is what builds the installers.
