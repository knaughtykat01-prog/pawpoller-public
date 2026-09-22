"""Two front-end contracts fixed in 4.32.1 (backlog UNIHASH, TGCHART).

Source-string tests, the same style as test_board_pages.py — the browser code has no runner here,
and both bugs are one token in one line, which is exactly what a string test can hold.

UNIHASH: a work's name travels in the hash percent-encoded and each renderer encodes it again for
its API call. For an ASCII name that double pass is the identity, which is why nobody noticed; a
title with an accent, a macron or CJK came back as `%25C5%258D…` and the page claimed the piece no
longer exists.

TGCHART: `metrics.snap` is the COLUMN the Overview chart reads out of an aggregate row
(`keys: [p.metrics.snap]`), not a label. Telegram declared `reactions` while its aggregate selects
`SUM(reactions_count) AS reactions_count`, so that chart could never draw.
"""
from __future__ import annotations

import re

import pytest

APP = open("frontend/js/app.js", encoding="utf-8").read()
PLATFORMS = open("frontend/js/platforms.js", encoding="utf-8").read()
TG_QUERIES = open("database/tg_queries.py", encoding="utf-8").read()


class TestNamesAreDecodedOnce:
    def test_the_router_has_a_single_decoding_helper(self):
        assert "const nameFrom = (i) =>" in APP
        assert "decodeURIComponent(raw)" in APP

    @pytest.mark.parametrize("call", [
        "Editor.renderEditor(nameFrom(1))",
        "window.Artwork.renderDetail(nameFrom(2))",
        "window.Masterpieces.renderDetail(nameFrom(1))",
        "StoryBoard.render(nameFrom(2))",
    ])
    def test_every_name_route_uses_it(self, call):
        assert call in APP, f"{call} must take its name through nameFrom()"

    def test_no_name_route_still_joins_raw_segments(self):
        """The shape that caused the bug: handing a renderer the raw, still-encoded tail."""
        offenders = re.findall(r"(?:renderDetail|renderEditor|StoryBoard\.render)\("
                               r"parts\.slice\(\d\)\.join\('/'\)\)", APP)
        assert not offenders, offenders

    def test_a_malformed_escape_does_not_throw(self):
        """`decodeURIComponent('%E0%A4%A')` throws URIError; a stray % in a name must not take the
        whole router down, so the helper falls back to the raw text."""
        helper = APP[APP.index("const nameFrom = (i) =>"):][:400]
        assert "catch" in helper and "return raw" in helper


class TestTelegramChartKey:
    def test_tg_snap_matches_the_column_its_aggregate_returns(self):
        decl = PLATFORMS[PLATFORMS.index("        tg:   M("):][:260]
        assert "'reactions_count'" in decl, "tg's snap must be the aggregate's column name"
        assert "SUM(reactions_count) AS reactions_count" in TG_QUERIES

    def test_the_chart_reads_snap_as_a_column(self):
        """If this contract ever changes, the test above is measuring the wrong thing."""
        assert "keys: [p.metrics.snap]" in APP

    @pytest.mark.parametrize("code, column", [
        ("ik", "likes"), ("bsky", "likes"), ("mast", "likes"),
        ("e621", "score"), ("tw", "views"), ("tum", "notes"),
    ])
    def test_the_other_engagement_platforms_still_line_up(self, code, column):
        """The same mismatch anywhere else would be just as invisible."""
        decl = PLATFORMS[PLATFORMS.index(f"        {code}:"):][:300]
        assert f"'{column}'" in decl
        sql = open(f"database/{code}_queries.py", encoding="utf-8").read()
        assert f"as {column}" in sql.lower()


class TestWelcomeCopy:
    """The first sentence a new user reads (backlog WELCOPY). It described the app as story
    analytics for fiction writers — from before art was made equal to writing, and before media
    pieces, the posts hub and the podcast feed existed at all."""

    def test_the_old_line_is_gone(self):
        assert "story analytics for furry fiction writers" not in APP

    def test_the_welcome_step_covers_what_the_app_actually_does(self):
        step = APP[APP.index("Welcome to PawPoller"):][:600]
        assert "art" in step.lower() and "fiction" in step.lower()
