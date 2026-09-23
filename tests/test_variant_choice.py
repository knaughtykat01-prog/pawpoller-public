"""Posting a NAMED render, not just the one the rating picks (backlog VARPICK).

Rating-driven routing only ever engages on a piece the site would refuse anyway. It
therefore cannot send an *alternate* render — a second colourway, a text-free version, a
different pose — because those are rated the same as the piece, so the primary always
"fits" and nothing is substituted. `variant_overrides` is how a caller says which one.

The rule that matters: naming a render chooses the FILE. It never relaxes the gate.
"""
from __future__ import annotations

import inspect

import pytest

from posting import artwork_reader, manager


@pytest.fixture
def piece(tmp_path):
    """A general-rated piece with two same-rated alternate renders."""
    for f in ("img.png", "alt.png", "text-free.png"):
        (tmp_path / f).write_bytes(b"\x89PNG\r\n\x1a\n")
    return artwork_reader.ArtworkInfo(
        name="Piece", path=tmp_path, title="Piece", description="", author="",
        rating="general", image="img.png",
        variants=[
            {"key": "alt", "label": "Alt colours", "image": "alt.png", "rating": "general"},
            {"key": "clean", "label": "Text-free", "image": "text-free.png", "rating": "general"},
        ])


def test_the_rating_alone_can_never_reach_an_alternate_render(piece):
    """Why this feature has to exist at all.

    Both renders are rated the same as the piece, so the primary always fits and
    `variant_for_rating` correctly declines to substitute — on every platform.
    """
    for ceiling in ("general", "mature", "adult"):
        assert artwork_reader.variant_for_rating(piece, ceiling) is None


def test_a_named_render_builds_from_that_file(piece):
    pkg = artwork_reader.build_artwork_package(piece, "fa", variant_key="clean")
    assert pkg.file_path.endswith("text-free.png")


def test_naming_a_render_does_not_relax_the_rating(tmp_path):
    """An adult render asked for by name is still adult when the gate reads it."""
    for f in ("img.png", "nude.png"):
        (tmp_path / f).write_bytes(b"\x89PNG\r\n\x1a\n")
    art = artwork_reader.ArtworkInfo(
        name="Piece", path=tmp_path, title="Piece", description="", author="",
        rating="general", image="img.png",
        variants=[{"key": "nude", "label": "Nude", "image": "nude.png", "rating": "adult"}])
    pkg = artwork_reader.build_artwork_package(art, "ig", variant_key="nude")
    assert pkg.rating == "adult"          # the gate sees adult and refuses on an SFW site

    from posting.platforms.base import rating_rank
    assert rating_rank(pkg.rating) > rating_rank("general")


class TestThePublishLoopHonoursIt:
    """Read off the source: the loop is async and needs a live poster to run."""

    def test_a_named_render_beats_the_ratings_pick(self):
        src = inspect.getsource(manager.post_artwork)
        assert 'variant_overrides or {}).get(platform)' in src
        # the automatic pick is the ELSE branch, so a name always wins
        assert src.index("_asked = ") < src.index("artwork_reader.variant_for_rating(")

    def test_primary_can_be_forced(self):
        """Without a sentinel there is no way to decline an automatic substitution."""
        assert manager._PRIMARY_RENDER == "__primary__"
        src = inspect.getsource(manager.post_artwork)
        assert "if _asked == _PRIMARY_RENDER:" in src
        assert "_variant = None" in src

    def test_an_unknown_render_name_refuses_that_platform(self):
        """Not a silent fallback to the primary: the caller asked for something real."""
        src = inspect.getsource(manager.post_artwork)
        assert "no render called" in src

    def test_the_gate_still_runs_on_a_named_render(self):
        src = inspect.getsource(manager.post_artwork)
        assert "poster.refusal(package)" in src
        assert src.index("_asked = ") < src.index("poster.refusal(package)")

    def test_the_route_passes_it_through(self):
        src = open("routes/artwork_api.py", encoding="utf-8").read()
        assert 'body.get("variant_overrides")' in src
        assert "variant_overrides=variant_overrides" in src


class TestWhichRenderWentOutIsRecorded:
    """`masterpiece_members.variant_key` existed and nothing ever wrote it, so every
    edit had to re-derive which render a site held — and re-deriving is wrong for an
    alternate render, which is rated the same as the piece and so derives to None."""

    def test_the_post_records_it(self):
        src = inspect.getsource(manager.post_artwork)
        assert 'variant_key=(_variant or {}).get("key") or ""' in src

    def test_the_edit_reads_it_back_before_re_deriving(self):
        src = inspect.getsource(manager.update_artwork)
        assert 'm.get("variant_key")' in src
        assert src.index('_recorded = ') < src.index("artwork_reader.variant_for_rating(")


def test_a_keyed_render_with_no_file_declared_never_posts_the_primary(tmp_path):
    """The mitigation from the 4.33.0 re-review.

    The rating is taken from the variant unconditionally, so a keyed variant with no
    resolvable `image` must not fall through to the primary's bytes — that is the
    original High's shape reached by a different door (a duplicate key in a hand-edited
    or partially-synced masterpiece.json, where the first match by key is the empty one).
    """
    (tmp_path / "img.png").write_bytes(b"ADULT")
    art = artwork_reader.ArtworkInfo(
        name="Piece", path=tmp_path, title="Piece", description="", author="",
        rating="adult", image="img.png",
        variants=[{"key": "sfw", "label": "SFW", "rating": "general"}])   # no image at all
    with pytest.raises(FileNotFoundError):
        artwork_reader.build_artwork_package(art, "ig", variant_key="sfw")


def test_a_duplicate_key_resolves_to_the_empty_one_and_still_refuses(tmp_path):
    """`build_artwork_package` re-resolves by key and takes the FIRST match, which may
    not be the dict the selector chose. It must refuse rather than post the primary."""
    (tmp_path / "img.png").write_bytes(b"ADULT")
    (tmp_path / "sfw.png").write_bytes(b"CLEAN")
    art = artwork_reader.ArtworkInfo(
        name="Piece", path=tmp_path, title="Piece", description="", author="",
        rating="adult", image="img.png",
        variants=[{"key": "sfw", "rating": "general"},                      # first: no image
                  {"key": "sfw", "image": "sfw.png", "rating": "general"}])  # second: real
    # The selector finds the real one...
    assert artwork_reader.variant_for_rating(art, "general")["image"] == "sfw.png"
    # ...but the build re-resolves to the first, and must not post the adult primary.
    with pytest.raises(FileNotFoundError):
        artwork_reader.build_artwork_package(art, "ig", variant_key="sfw")

def test_a_non_dict_in_variants_does_not_crash_the_run(tmp_path):
    """A junk entry used to raise AttributeError out of the per-platform loop, taking
    every remaining platform with it and skipping the watermark temp cleanup."""
    (tmp_path / "img.png").write_bytes(b"PNGBYTES")
    (tmp_path / "alt.png").write_bytes(b"PNGBYTES")
    art = artwork_reader.ArtworkInfo(
        name="Piece", path=tmp_path, title="Piece", description="", author="",
        rating="general", image="img.png",
        variants=["junk", {"key": "alt", "image": "alt.png", "rating": "general"}])
    pkg = artwork_reader.build_artwork_package(art, "fa", variant_key="alt")
    assert pkg.file_path.endswith("alt.png")


def test_an_edit_refuses_when_the_recorded_render_is_gone():
    """Never fall through to the primary: an edit keeps the live bytes
    (skip_content_refresh), so pushing the primary's rating over a submission holding a
    different render decouples the rating from the image — the first BLOCK's shape,
    reached from the edit side."""
    src = inspect.getsource(manager.update_artwork)
    block = src[src.index("_recorded = "):src.index("else:", src.index("_recorded = "))]
    assert "no longer declares" in block
    assert "continue" in block
