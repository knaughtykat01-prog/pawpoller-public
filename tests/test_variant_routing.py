"""Each site gets the render it can actually take (backlog VARPOST).

A piece and its variants are one artwork at different ratings — the catalogue holds SFW,
Censored, Nude and Cum renders of the same image. `build_artwork_package` has accepted a
`variant_key` since 3.2.0 and nothing ever passed one, so a publish always sent the
PRIMARY render: an adult piece was simply refused on Instagram or Threads even with an
SFW render sitting beside it.

The second half of this module is the security review's High finding, pinned: selection
and substitution have to agree about which files exist, or a missing SFW render posts the
ADULT primary's bytes under the label `general`.
"""
from __future__ import annotations

import pytest

from posting import artwork_reader


class _Art:
    """The shape variant_for_rating reads off an ArtworkInfo."""

    def __init__(self, rating, variants, path=None, name="Piece"):
        self.rating = rating
        self.variants = variants
        self.path = path
        self.name = name


SFW = {"key": "sfw", "label": "SFW", "image": "sfw.png", "rating": "general"}
CENSORED = {"key": "censored", "label": "Censored", "image": "cen.png", "rating": "mature"}
NUDE = {"key": "nude", "label": "Nude", "image": "nude.png", "rating": "adult"}


@pytest.fixture
def renders(tmp_path):
    """A piece's folder with every declared render actually on disk.

    Every test below passes this: a variant is a FILE, and a fixture that pretends
    otherwise is how the High finding survived the first round of tests.
    """
    for f in ("img.png", "sfw.png", "cen.png", "nude.png", "alt.png", "o.png", "x.png"):
        (tmp_path / f).write_bytes(b"\x89PNG\r\n\x1a\n")
    return tmp_path


def test_a_piece_the_site_takes_posts_as_itself(renders):
    art = _Art("general", [SFW, CENSORED], renders)
    assert artwork_reader.variant_for_rating(art, "adult") is None
    assert artwork_reader.variant_for_rating(art, "general") is None


def test_an_adult_piece_sends_its_sfw_render_to_an_sfw_site(renders):
    art = _Art("adult", [SFW, CENSORED], renders)
    assert artwork_reader.variant_for_rating(art, "general") == SFW


def test_a_mature_site_gets_the_fullest_render_it_allows(renders):
    """The censored render beats the SFW one where both are allowed."""
    art = _Art("adult", [SFW, CENSORED], renders)
    assert artwork_reader.variant_for_rating(art, "mature") == CENSORED


def test_nothing_suitable_stays_a_refusal(renders):
    """Posting something the site rejects is worse than the gate's clear refusal."""
    art = _Art("adult", [NUDE], renders)
    assert artwork_reader.variant_for_rating(art, "general") is None


def test_a_variant_with_no_rating_of_its_own_inherits_the_piece(renders):
    art = _Art("adult", [{"key": "alt", "label": "Alt", "image": "alt.png"}], renders)
    assert artwork_reader.variant_for_rating(art, "general") is None


def test_half_declared_variants_are_ignored(renders):
    art = _Art("adult", [{"key": "", "image": "x.png", "rating": "general"},
                         {"key": "nofile", "rating": "general"},
                         "not-a-dict"], renders)
    assert artwork_reader.variant_for_rating(art, "general") is None


def test_an_unknown_rating_word_counts_as_adult(renders):
    """rating_rank's rule: never leak something explicit by mislabelling."""
    art = _Art("adult", [{"key": "odd", "image": "o.png", "rating": "spicy"}], renders)
    assert artwork_reader.variant_for_rating(art, "general") is None


class TestARenderThatIsNotThere:
    """The High from the 4.33.0 security review.

    `variant_for_rating` used to select on `image` being a non-empty STRING, while
    `build_artwork_package` took the variant's RATING unconditionally and swapped the
    IMAGE only when the file existed — logging a warning and keeping the primary when it
    didn't. The package then carried the adult primary's bytes labelled `general`, which
    is precisely what the rating gate reads. A render deleted, renamed, case-mismatched
    between Windows and Linux, or not yet carried across by a partial sync was enough.
    """

    def test_a_missing_render_is_never_selected(self, renders):
        (renders / "sfw.png").unlink()
        art = _Art("adult", [SFW], renders)
        # None means the rating gate refuses in its own words — the safe outcome.
        assert artwork_reader.variant_for_rating(art, "general") is None

    def test_the_fullest_render_still_on_disk_wins(self, renders):
        """A gone render falls back to a real lesser one, not to the primary."""
        (renders / "cen.png").unlink()
        art = _Art("adult", [SFW, CENSORED], renders)
        assert artwork_reader.variant_for_rating(art, "mature") == SFW

    def test_a_render_outside_the_pieces_folder_is_ignored(self, renders, tmp_path):
        """`image` is a stored string, so it must be anchored like the artwork name is."""
        outside = tmp_path.parent / "elsewhere.png"
        outside.write_bytes(b"\x89PNG\r\n\x1a\n")
        escape = {"key": "esc", "image": f"../{outside.name}", "rating": "general"}
        assert artwork_reader.variant_image_path(_Art("adult", [escape], renders), escape) is None
        assert artwork_reader.variant_for_rating(_Art("adult", [escape], renders), "general") is None

    def test_variant_image_path_finds_a_render_that_is_there(self, renders):
        got = artwork_reader.variant_image_path(_Art("adult", [SFW], renders), SFW)
        assert got is not None and got.name == "sfw.png"

    def test_an_explicitly_asked_for_render_that_is_gone_raises(self, renders):
        """Never a silent downgrade to the primary: the caller named a render.

        The rating has already been taken from the variant by this point, so returning
        the primary's bytes is the leak. Refusing is the only safe answer, and the
        publish loop turns it into one refused platform rather than a dead run.
        """
        art = artwork_reader.ArtworkInfo(
            name="Piece", path=renders, title="Piece", description="", author="",
            rating="adult", image="img.png", variants=[SFW])
        (renders / "sfw.png").unlink()

        with pytest.raises(FileNotFoundError) as e:
            artwork_reader.build_artwork_package(art, "ig", variant_key="sfw")
        assert "sfw.png" in str(e.value)

    def test_the_same_build_succeeds_while_the_render_is_there(self, renders):
        """The guard refuses a GONE render, not every render."""
        art = artwork_reader.ArtworkInfo(
            name="Piece", path=renders, title="Piece", description="", author="",
            rating="adult", image="img.png", variants=[SFW])
        pkg = artwork_reader.build_artwork_package(art, "ig", variant_key="sfw")
        assert pkg.rating == "general"
        assert pkg.file_path.endswith("sfw.png")   # the SFW bytes, not the primary's


class TestThePublishPathUsesIt:
    def test_the_manager_asks_before_building_the_package(self):
        src = open("posting/manager.py", encoding="utf-8").read()
        assert "artwork_reader.variant_for_rating(" in src
        block = src[src.index("_variant = artwork_reader.variant_for_rating("):][:900]
        assert 'variant_key=(_variant or {}).get("key") or None' in block
        assert 'getattr(poster, "max_rating", "adult")' in block

    def test_the_result_says_which_render_went_out(self):
        """Silently posting a different image than the one on screen would be worse
        than not doing it at all — every result carries the render's name."""
        src = open("posting/manager.py", encoding="utf-8").read()
        # refusal + post on the way out, and the edit path's own refusal (4.33.0)
        assert src.count('"variant": (_variant or {}).get("label")') == 3

    def test_a_missing_render_refuses_one_platform_not_the_run(self):
        src = open("posting/manager.py", encoding="utf-8").read()
        assert src.count("except FileNotFoundError as e:") == 2  # post + edit paths

    def test_the_edit_path_picks_the_same_render_and_runs_the_gate(self):
        """An edit used to push the PRIMARY's rating and tags to a site holding the SFW
        render — `update_artwork` had neither variant selection nor a `refusal()` call."""
        src = open("posting/manager.py", encoding="utf-8").read()
        block = src[src.index("async def update_artwork"):]
        assert "artwork_reader.variant_for_rating(" in block
        assert "poster.refusal(package)" in block
