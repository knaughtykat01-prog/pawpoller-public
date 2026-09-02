"""Per-platform tag budgets, in one place (3.12.0).

The canonical tag set is meant to be RICH. `core` is a priority ORDER, not the
subset that gets posted: every platform is offered the whole canonical list and
trims from the TAIL, so the tags declared most important survive everywhere.

Before this, `_TAG_BUDGET` knew three platforms (fa, ao3, sqw) while three
posters capped tags themselves in a second place — and one of those caps was
enforcing the wrong rule entirely:

    tags=package.tags[:59],  # Max 59 chars per tag      <- itaku.py

59 is Itaku's per-tag CHARACTER limit. Slicing the tag LIST to 59 items invents
a limit that does not exist, silently drops tags from any work with more than 59
of them, and still lets a 60-character tag through to be rejected. Wrong in both
directions, in two call sites.
"""
from __future__ import annotations

import pytest

from posting import tag_budget as tb


# ── the bug ──────────────────────────────────────────────────────

def test_itaku_limits_tag_LENGTH_not_tag_COUNT():
    """The regression this module exists for."""
    many = [f"tag{i}" for i in range(80)]
    assert len(tb.fit(many, "ik")) == 80, "80 short tags are fine — Itaku has no count limit"

    long_one = ["ok_tag", "x" * 60, "also_fine"]
    assert tb.fit(long_one, "ik") == ["ok_tag", "also_fine"], "the 60-char tag is what must go"


def test_a_tag_exactly_on_the_per_tag_limit_is_kept():
    assert tb.fit(["y" * 59], "ik") == ["y" * 59]


def test_an_over_long_tag_is_dropped_not_truncated():
    """Truncating changes what a tag means and can collide with a real one;
    dropping loses exactly the tag the platform would have rejected."""
    out = tb.fit(["a" * 70], "ik")
    assert out == []
    assert "a" * 59 not in out


# ── the limits themselves ────────────────────────────────────────

@pytest.mark.parametrize("platform,limit", [
    ("sf", 97),     # SoFurry — the one that was missing entirely
    ("da", 30),
    ("wp", 24),
    ("ao3", 75),
    ("sqw", 75),
])
def test_count_limits_come_from_the_documented_table(platform, limit):
    assert len(tb.fit([f"t{i}" for i in range(200)], platform)) == limit


def test_furaffinity_is_limited_by_characters_not_count():
    """FA's field is a 500-character keyword string; the tag count is unlimited,
    so a hundred short tags are fine and twenty long ones are not."""
    short = [f"t{i}" for i in range(100)]
    assert len(tb.fit(short, "fa")) == 100

    long = ["averyverylongtagindeed" * 2 for _ in range(40)]
    out = tb.fit(long, "fa")
    assert len(" ".join(out)) <= 500


@pytest.mark.parametrize("platform", ["ib", "e621", "ws", "fn", "fbr"])
def test_unlimited_platforms_pass_everything_through(platform):
    """Inkbunny was measured at 108+ keywords; inventing a cap would throw away
    tags for nothing."""
    many = [f"t{i}" for i in range(150)]
    assert tb.fit(many, platform) == many


# ── ordering ─────────────────────────────────────────────────────

def test_trimming_drops_from_the_tail():
    """The whole point of core-first ordering: what survives is what was
    declared to matter."""
    tags = [f"core{i}" for i in range(10)] + [f"aux{i}" for i in range(40)]
    out = tb.fit(tags, "da")           # 30 max
    assert out[:10] == [f"core{i}" for i in range(10)]
    assert out == tags[:30]


def test_order_is_never_reshuffled():
    tags = ["zebra", "anthro", "male"]
    assert tb.fit(tags, "sf") == tags


# ── reporting ────────────────────────────────────────────────────

def test_a_budget_that_bites_into_core_is_logged(caplog):
    """A trim that reaches past the core set is a tagging problem the user has
    to be told about, not something to paper over."""
    import logging
    with caplog.at_level(logging.WARNING):
        tb.fit([f"t{i}" for i in range(60)], "da", core_count=40)
    assert "cut into the core set" in caplog.text


def test_itaku_minimum_is_warned_not_padded(caplog):
    """Inventing tags to satisfy a minimum is worse than a clear failure."""
    import logging
    with caplog.at_level(logging.WARNING):
        out = tb.fit(["a_tag", "b_tag"], "ik")
    assert out == ["a_tag", "b_tag"], "nothing invented"
    assert "at least 5" in caplog.text


def test_preview_reports_what_each_platform_loses():
    tags = [f"t{i}" for i in range(40)]
    p = tb.preview(tags, "da")
    assert p["sent"] == 30 and p["total"] == 40
    assert p["dropped"] == [f"t{i}" for i in range(30, 40)]
    assert p["limit"] == "30 tags max"


def test_preview_on_an_unlimited_platform_loses_nothing():
    tags = [f"t{i}" for i in range(40)]
    p = tb.preview(tags, "ib")
    assert p["sent"] == 40 and p["dropped"] == []
    assert p["limit"] == "no limit"


# ── one source of truth ──────────────────────────────────────────

def test_no_poster_caps_tags_by_hand_any_more():
    """A second place that trims is a second place to get it wrong — which is
    exactly how the Itaku bug survived."""
    import pathlib
    import re
    bad = []
    for f in pathlib.Path("posting/platforms").glob("*.py"):
        for m in re.finditer(r"package\.tags\[:\d+\]", f.read_text(encoding="utf-8")):
            bad.append(f"{f.name}: {m.group(0)}")
    assert bad == [], f"hard-coded tag caps outside tag_budget: {bad}"


def test_artwork_reader_delegates_rather_than_duplicating():
    from posting import artwork_reader as ar
    assert ar.fit_tags_to_platform([f"t{i}" for i in range(50)], "da") == \
        tb.fit([f"t{i}" for i in range(50)], "da")
    assert ar._TAG_BUDGET is tb.BUDGETS
