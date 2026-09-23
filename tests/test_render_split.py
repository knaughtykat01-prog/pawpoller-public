"""One submission per render (backlog VARSPLIT, spec 004).

4.33.0 could route a render by rating and name one per site, but both still produced
ONE submission per platform. This is the rest: a piece holding several versions becomes
several posts, each its own submission with its own URL and history.

The loop is nested platform-OUTER, render-inner. That is not arbitrary — it is what makes
the rate limit between renders the right one, because `min_post_interval` is a property
of the platform and FurAffinity enforces 70 seconds.
"""
from __future__ import annotations

import inspect

import pytest

from posting import artwork_reader, manager


@pytest.fixture
def piece(tmp_path):
    for f in ("img.png", "alt.png", "clean.png"):
        (tmp_path / f).write_bytes(b"PNGBYTES")
    return artwork_reader.ArtworkInfo(
        name="Piece", path=tmp_path, title="A Piece", description="", author="",
        rating="general", image="img.png",
        variants=[
            {"key": "alt", "label": "Alt colours", "image": "alt.png", "rating": "general"},
            {"key": "clean", "label": "Text-free", "image": "clean.png", "rating": "general"},
        ])


class TestTheLoopShape:
    """Read off the source — the loop is async and needs live posters to run."""

    def test_the_render_loop_is_nested_inside_the_platform_loop(self):
        src = inspect.getsource(manager.post_artwork)
        assert src.index("for platform in _announcers_last(platforms):") \
            < src.index("for _ri, _variant in enumerate(_renders):"), \
            "platform must be the OUTER loop or the rate limit below is the wrong one"

    def test_it_rate_limits_between_renders(self):
        """FR-007. post_artwork was the only publish path that never rate-limited —
        harmless while each platform got one post, a live problem the moment
        FurAffinity gets three against its enforced 70-second floor."""
        src = inspect.getsource(manager.post_artwork)
        assert "await poster._rate_limit()" in src
        block = src[src.index("for _ri, _variant in enumerate(_renders):"):][:400]
        assert "if _ri:" in block, "no sleep before the FIRST render"

    def test_the_gate_runs_per_render(self):
        """FR-002: one render refused must not refuse its siblings."""
        src = inspect.getsource(manager.post_artwork)
        assert src.index("for _ri, _variant in enumerate(_renders):") \
            < src.index("poster.refusal(package)")

    def test_each_publication_row_records_its_render(self):
        """Three places have to agree about which render this was: the publication row,
        the masterpiece member, and any retry queued for it. A retry that forgets comes
        back as the rating's pick, which for an alternate render is the primary."""
        src = inspect.getsource(manager.post_artwork)
        for call in ("upsert_publication(", "add_member(", "_schedule_retry("):
            i = src.index(call)
            block = src[i:i + 700]
            assert "variant_key=" in block, f"{call} does not carry the render"

    def test_an_unknown_render_in_the_list_is_reported_not_guessed(self):
        src = inspect.getsource(manager.post_artwork)
        assert 'no render called {_key!r} on this piece' in src

    def test_renders_outranks_a_named_override(self):
        src = inspect.getsource(manager.post_artwork)
        assert src.index("if renders:") < src.index("elif _asked == _PRIMARY_RENDER:")

    def test_the_route_passes_the_list(self):
        src = open("routes/artwork_api.py", encoding="utf-8").read()
        assert 'body.get("renders")' in src
        assert "renders=renders" in src
        assert "must be a list" in src, "a JSON string would AttributeError into a 500"


class TestTheTitleSuffix:
    """FR — two renders on a gallery need names a VIEWER can tell apart."""

    def test_a_gallery_gets_the_render_in_the_title(self, piece):
        pkg = artwork_reader.build_artwork_package(
            piece, "fa", variant_key="alt", multi_render=True)
        assert pkg.title == "A Piece (Alt colours)"

    def test_a_single_render_reads_as_the_piece(self, piece):
        """Posted alone it is the piece, not a variant of it."""
        pkg = artwork_reader.build_artwork_package(
            piece, "fa", variant_key="alt", multi_render=False)
        assert pkg.title == "A Piece"

    def test_a_booru_is_untouched(self, piece):
        """e621 has no title field at all and Furbooru posts only a description, so
        there is nothing there to collide — renders are told apart by their tags."""
        for code in ("e621", "fbr"):
            pkg = artwork_reader.build_artwork_package(
                piece, code, variant_key="alt", multi_render=True)
            assert pkg.title == "A Piece"

    def test_the_label_falls_back_to_the_key(self, tmp_path):
        for f in ("img.png", "x.png"):
            (tmp_path / f).write_bytes(b"PNGBYTES")
        art = artwork_reader.ArtworkInfo(
            name="Piece", path=tmp_path, title="A Piece", description="", author="",
            rating="general", image="img.png",
            variants=[{"key": "sketch", "image": "x.png", "rating": "general"}])
        pkg = artwork_reader.build_artwork_package(
            art, "fa", variant_key="sketch", multi_render=True)
        assert pkg.title == "A Piece (sketch)"

    def test_an_explicit_title_override_still_wins(self, piece):
        pkg = artwork_reader.build_artwork_package(
            piece, "fa", variant_key="alt", multi_render=True, title_override="Mine")
        assert pkg.title == "Mine"

    def test_a_label_already_in_the_title_is_not_repeated(self, tmp_path):
        for f in ("img.png", "x.png"):
            (tmp_path / f).write_bytes(b"PNGBYTES")
        art = artwork_reader.ArtworkInfo(
            name="Piece", path=tmp_path, title="A Piece - Sketch", description="",
            author="", rating="general", image="img.png",
            variants=[{"key": "sketch", "label": "Sketch", "image": "x.png"}])
        pkg = artwork_reader.build_artwork_package(
            art, "fa", variant_key="sketch", multi_render=True)
        assert pkg.title == "A Piece - Sketch"

    def test_the_primary_never_gets_a_suffix(self, piece):
        pkg = artwork_reader.build_artwork_package(piece, "fa", multi_render=True)
        assert pkg.title == "A Piece"


class TestTheQueueCarriesIt:
    """D3: without this a queued or retried render post silently becomes the rating's
    pick — and for an alternate render, rated the same as the piece, that is the
    primary."""

    def test_add_to_queue_takes_a_render(self):
        from database import posting_queries
        sig = inspect.signature(posting_queries.add_to_queue)
        assert "variant_key" in sig.parameters

    def test_the_scheduler_passes_it_on(self):
        src = open("posting/scheduler.py", encoding="utf-8").read()
        assert "variant_overrides=" in src, "a queued render post must name its render"


class TestSeveralSubmissionsOnOneSite:
    """T018: nothing downstream may assume one upload per platform any more."""

    def test_get_members_returns_every_render(self, tmp_path):
        import sqlite3
        from database import masterpiece_queries as mq
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("""CREATE TABLE masterpieces (
            name TEXT PRIMARY KEY, status TEXT DEFAULT 'active',
            source_link_id INTEGER,
            updated_at TEXT DEFAULT (datetime('now')))""")
        conn.execute("""CREATE TABLE masterpiece_members (
            masterpiece_name TEXT, platform TEXT, submission_id TEXT,
            account_id INTEGER, role TEXT DEFAULT 'crosspost',
            linked_via TEXT DEFAULT 'manual', variant_key TEXT NOT NULL DEFAULT '',
            added_at TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (masterpiece_name, platform, submission_id))""")
        for sid, vk in (("1", ""), ("2", "alt")):
            mq.add_member(conn, "Piece", "fa", sid, account_id=1, variant_key=vk)

        rows = mq.get_members(conn, "Piece")
        assert len(rows) == 2, "two renders on ONE platform are two members"
        assert {r["variant_key"] for r in rows} == {"", "alt"}
        # and the filter still narrows to one
        assert len(mq.get_members(conn, "Piece", variant_key="alt")) == 1

    def test_the_edit_path_walks_members_not_platforms(self):
        """`update_artwork` loops the member rows, so several uploads on one site are
        edited individually — each with the render IT holds."""
        src = inspect.getsource(manager.update_artwork)
        assert "for m in members:" in src
        assert 'm.get("variant_key")' in src

    def test_the_locations_payload_says_which_render(self):
        src = open("database/masterpiece_queries.py", encoding="utf-8").read()
        assert 'loc["variant_key"] = m.get("variant_key") or ""' in src


class TestAutomaticStaysTheDefault:
    """The picker is sent for EVERY piece that has a render, so its default tick decides
    what happens to the 4.33.0 rating routing. A default of "the original" would switch
    that routing off for exactly the pieces it exists to serve."""

    def test_auto_is_a_distinct_value_from_the_primary(self):
        assert manager._AUTO_RENDER == "__auto__"
        assert manager._PRIMARY_RENDER == "__primary__"
        assert manager._AUTO_RENDER != manager._PRIMARY_RENDER

    def test_auto_resolves_to_the_ratings_pick(self):
        src = inspect.getsource(manager.post_artwork)
        block = src[src.index("if _key == _AUTO_RENDER:"):][:300]
        assert "variant_for_rating(" in block

    def test_the_dialog_ticks_automatic_not_the_original(self):
        src = open("frontend/js/artwork.js", encoding="utf-8").read()
        block = src[src.index("_pubRenders(meta) {"):][:800]
        assert "'__auto__', checked: true" in block
        i_auto, i_prim = block.index("__auto__"), block.index("__primary__")
        assert i_auto < i_prim, "automatic is the first and default choice"
        assert "checked: true" not in block[i_prim:], "the original must NOT be pre-ticked"

    def test_a_render_chosen_twice_posts_once(self):
        """Auto can resolve to a render the user also ticked by name."""
        src = inspect.getsource(manager.post_artwork)
        assert "_seen, _uniq = set(), []" in src


class TestTheReviewFindings:
    """4.34.0's security review. The High is pinned behaviourally in
    tests/test_publications_variant_migration.py; these are the rest."""

    def test_the_desktop_queue_fallback_names_the_render(self):
        """N renders failing a server post used to queue N rows that all meant "the
        rating's pick" — the desktop then published the same image N times and the
        alternates never went out. posting_queue has no UNIQUE to dedupe them."""
        src = inspect.getsource(manager.post_artwork)
        i = src.index('requires="desktop"')
        # the kwarg follows the comment block explaining it, so look further ahead
        assert "variant_key=" in src[i:i + 700]

    def test_the_renders_list_is_capped(self):
        """Unknown keys append a refusal per platform BEFORE the de-duplication, so an
        uncapped list is ~len × platforms result dicts."""
        src = open("routes/artwork_api.py", encoding="utf-8").read()
        assert "len(renders) > 32" in src

    def test_a_null_render_is_rejected_not_read_as_the_primary(self):
        """`null` would match any variant dict lacking a "key" and quietly mean the
        primary — a silent reading of a malformed request."""
        src = open("routes/artwork_api.py", encoding="utf-8").read()
        assert "isinstance(r, str) for r in renders" in src
