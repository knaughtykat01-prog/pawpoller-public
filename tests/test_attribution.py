"""'Posted via PawPoller' attribution line (gap-wave-2 §1) + artwork alt-text (G6).

Covers maybe_append's gating (on by default, off, bsky skip, idempotency) and
that build_artwork_package actually applies it — plus the G6 alt_text
pass-through into package.extra with title fallback at the poster.
"""
from pathlib import Path

import config
from posting import attribution
from posting.artwork_reader import ArtworkInfo, build_artwork_package


def test_on_by_default_and_appends():
    out = attribution.maybe_append("My description.", "ib")
    assert out.endswith(attribution.ATTRIBUTION_LINE)
    assert out.startswith("My description.")


def test_off_when_disabled():
    config.save_settings({"pawpoller_attribution": False})
    assert attribution.maybe_append("My description.", "ib") == "My description."


def test_bsky_is_skipped():
    assert attribution.maybe_append("Announcement text", "bsky") == "Announcement text"


def test_never_double_appends():
    once = attribution.maybe_append("Desc", "fa")
    twice = attribution.maybe_append(once, "fa")
    assert twice == once
    # A user's own hand-typed credit also blocks the append.
    manual = "My thing. Posted via PawPoller, btw."
    assert attribution.maybe_append(manual, "fa") == manual


def test_empty_description_gets_bare_line():
    assert attribution.maybe_append("", "ws") == attribution.ATTRIBUTION_LINE


def _art(**kw):
    base = dict(name="Test_Piece", path=Path("."), title="Test Piece",
                description="A piece.", author="A", rating="general", image="a.png")
    base.update(kw)
    return ArtworkInfo(**base)


def test_artwork_package_carries_attribution_and_alt():
    art = _art(alt_text="A wolf grinning")
    pkg = build_artwork_package(art, "ib")
    assert attribution.ATTRIBUTION_LINE in pkg.description
    assert pkg.extra["alt_text"] == "A wolf grinning"          # G6 pass-through

    # Disabled → clean description; empty alt → no extra key (poster falls
    # back to the title).
    config.save_settings({"pawpoller_attribution": False})
    pkg2 = build_artwork_package(_art(), "ib")
    assert attribution.ATTRIBUTION_LINE not in pkg2.description
    assert "alt_text" not in pkg2.extra
