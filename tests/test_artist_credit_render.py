"""Rendering the artist credit in each platform's own markup.

These tests pin markup that was verified against each platform's own docs or
shipped frontend. They are the guard against the failure the feature exists to
fix: posting a raw furaffinity.net URL *to FurAffinity*, where it renders as
inert text instead of a real user link.

The two rules that matter most, and why each has its own test:
  * a credit is NEVER silently dropped — no handle, unknown platform and
    unverified markup all degrade to a plain "Art by <name>";
  * FurAffinity handles are emitted in FA's STRIPPED slug form, because
    /user/long_eared_hare is a 400 while /user/longearedhare is a 200.
"""
from __future__ import annotations

import pytest

from posting import artist_credit as ac

INKWOLF = {"name": "Inkwolf", "handles": {"fa": "inkwolf"}}
NAME_ONLY = {"name": "Nine Tails", "handles": {}}


# --------------------------------------------------------------- native forms
@pytest.mark.parametrize("platform,expected", [
    ("fa",   "Art by :iconinkwolf:"),
    ("ib",   "Art by [fa]inkwolf[/fa]"),
    ("ws",   "Art by <fa:inkwolf>"),
    ("e621", 'Art by "Inkwolf":https://www.furaffinity.net/user/inkwolf'),
    ("fn",   "Art by [Inkwolf](https://www.furaffinity.net/user/inkwolf)"),
    ("ik",   "Art by [Inkwolf](https://www.furaffinity.net/user/inkwolf)"),
    ("bsky", "Art by Inkwolf"),
    # DA drops the URL — measured 2026-08-22, see test_da_offsite_links.py.
    # DeviantArt deletes off-site links from a description AND the paragraph
    # they sit on, so this line used to arrive as NOTHING rather than as an
    # unclickable URL. Changed deliberately in 3.23.0; do not "restore" it.
    ("da",   "Art by Inkwolf"),
    ("sf",   "Art by Inkwolf - https://www.furaffinity.net/user/inkwolf"),
    ("tw",   "Art by Inkwolf - https://www.furaffinity.net/user/inkwolf"),
])
def test_fa_artist_rendered_per_platform(platform, expected):
    assert ac.render(INKWOLF, platform) == expected


@pytest.mark.parametrize("platform,expected", [
    ("ib",   "Art by [name]boo[/name]"),
    ("fa",   "Art by [url=https://inkbunny.net/boo]Sablejay[/url]"),
    ("ws",   "Art by <ib:boo>"),
])
def test_same_site_handle_wins(platform, expected):
    """An InkBunny artist gets IB's native user link ON InkBunny."""
    artist = {"name": "Sablejay", "handles": {"ib": "boo"}}
    assert ac.render(artist, platform) == expected


def test_weasyl_native_is_avatar_plus_name():
    artist = {"name": "Foo", "handles": {"ws": "foo"}}
    assert ac.render(artist, "ws") == "Art by <!~foo>"


@pytest.mark.parametrize("platform", ["fn", "ik"])
def test_same_site_mention(platform):
    artist = {"name": "Foo", "handles": {platform: "foo_art"}}
    assert ac.render(artist, platform) == "Art by @foo_art"


def test_bluesky_mention_needs_a_dotted_handle():
    """The client's facet regex requires a dot, so a bare alias can't mention."""
    dotted = {"name": "Foo", "handles": {"bsky": "foo.bsky.social"}}
    bare = {"name": "Foo", "handles": {"bsky": "foo"}}
    assert ac.render(dotted, "bsky") == "Art by @foo.bsky.social"
    assert ac.render(bare, "bsky") == "Art by Foo"


def test_bluesky_never_appends_a_url():
    """300-grapheme cap: the credit must not eat the blurb."""
    assert "http" not in ac.render(INKWOLF, "bsky")


# ------------------------------------------------------- FA slug normalisation
@pytest.mark.parametrize("handle,expected", [
    ("long_eared_hare", ":iconlongearedhare:"),
    ("Long_Eared_Hare", ":iconlongearedhare:"),
    ("PINEFOX463ART", ":iconpinefox463art:"),
    ("dan.thornfield", ":icondanthornfield:"),
    ("x-grey-ember-x", ":iconxgreyemberx:"),
])
def test_fa_handles_are_stripped_to_the_url_form(handle, expected):
    assert ac.render({"name": "N", "handles": {"fa": handle}}, "fa") == f"Art by {expected}"


def test_fa_slug_also_applied_to_cross_site_links():
    """An FA handle linked FROM another site must still use the URL form."""
    artist = {"name": "N", "handles": {"fa": "Long_Eared_Hare"}}
    assert ac.render(artist, "ib") == "Art by [fa]longearedhare[/fa]"
    assert ac.profile_url(artist) == "https://www.furaffinity.net/user/longearedhare"


def test_unstrippable_fa_handle_falls_back_to_the_name():
    """A handle of pure punctuation must not emit ':icon:'."""
    assert ac.render({"name": "N", "handles": {"fa": "___"}}, "fa") == "Art by N"


# --------------------------------------------------------- never lose a credit
@pytest.mark.parametrize("platform", ["fa", "ib", "ws", "e621", "fn", "ik", "bsky",
                                      "da", "sf", "tw", "tum", "mast", "ig", "wat"])
def test_name_only_artist_always_credited(platform):
    assert ac.render(NAME_ONLY, platform) == "Art by Nine Tails"


def test_unknown_platform_degrades_to_plain():
    assert ac.render(INKWOLF, "not_a_platform") == \
        "Art by Inkwolf - https://www.furaffinity.net/user/inkwolf"


@pytest.mark.parametrize("artist", [None, {}, {"name": "", "handles": {}}])
def test_no_artist_renders_nothing(artist):
    assert ac.render(artist, "fa") == ""


def test_handle_without_a_name_uses_the_handle_as_the_name():
    assert ac.render({"name": "", "handles": {"fa": "inkwolf"}}, "fa") == "Art by :iconinkwolf:"


def test_custom_prefix():
    assert ac.render(NAME_ONLY, "fa", prefix="Done by") == "Done by Nine Tails"


# ----------------------------------------------------------------- append_to
def test_append_adds_a_blank_line():
    assert ac.append_to("A blurb.", INKWOLF, "fa") == "A blurb.\n\nArt by :iconinkwolf:"


def test_append_is_idempotent_on_the_name():
    """Guards a double-append when the description still carries a credit."""
    once = ac.append_to("A blurb.", INKWOLF, "fa")
    assert ac.append_to(once, INKWOLF, "fa") == once


def test_append_skips_when_the_blurb_already_names_the_artist():
    desc = "A gift from Inkwolf for my birthday."
    assert ac.append_to(desc, INKWOLF, "fa") == desc


def test_append_to_empty_description_is_just_the_credit():
    assert ac.append_to("", INKWOLF, "fa") == "Art by :iconinkwolf:"
    assert ac.append_to("   ", INKWOLF, "fa") == "Art by :iconinkwolf:"


def test_append_without_an_artist_is_a_no_op():
    assert ac.append_to("A blurb.", None, "fa") == "A blurb."


# ---------------------------------------------------------------- artist tag
@pytest.mark.parametrize("name,expected", [
    ("Inkwolf", "inkwolf"),
    ("Nine Tails", "nine_tails"),
    ("Juniper Vale", "juniper_vale"),
    ("Sablejay", "sablejay"),
    ("  Spaced  Out  ", "spaced_out"),
])
def test_artist_tag(name, expected):
    assert ac.artist_tag({"name": name, "handles": {}}) == expected


@pytest.mark.parametrize("artist", [None, {}, {"name": "  "}, {"handles": {"fa": "x"}}])
def test_artist_tag_empty_when_no_name(artist):
    assert ac.artist_tag(artist) == ""


# --------------------------------------------------------------- profile_url
def test_profile_url_prefers_the_same_site():
    artist = {"name": "N", "handles": {"fa": "faname", "tw": "twname"}}
    assert ac.profile_url(artist, "tw") == "https://twitter.com/twname"
    assert ac.profile_url(artist, "fa") == "https://www.furaffinity.net/user/faname"


def test_profile_url_empty_without_handles():
    assert ac.profile_url(NAME_ONLY, "fa") == ""
    assert ac.profile_url(None, "fa") == ""


# --------------------------------------------------------------- sanitising
# The name and handle are user data interpolated into six markup languages, so
# a surviving delimiter breaks out of the credit line. This needs no attacker:
# a credit written `Art by "Inkwolf"` leaves quotes on the name, and booru
# handles commonly take the `name_(artist)` shape.

@pytest.mark.parametrize("platform,name,must_not_contain", [
    ("fa",   "Ink[wolf]",                      ["[wolf]"]),
    ("ib",   "Ink[wolf]",                      ["[wolf]"]),
    ("e621", '"Inkwolf"',                      ['""']),
    ("fn",   "x](https://evil.example) [real", ["](https://evil.example)"]),
    ("ws",   "x](https://evil.example) [real", ["](https://evil.example)"]),
    ("da",   "<Sam>",                          ["<", ">"]),
])
def test_name_delimiters_cannot_break_out(platform, name, must_not_contain):
    got = ac.render({"name": name, "handles": {"tw": "someone"}}, platform)
    for frag in must_not_contain:
        assert frag not in got, got


@pytest.mark.parametrize("platform", ["fa", "ib", "ws", "fn", "e621"])
def test_handle_delimiters_cannot_break_out(platform):
    """The breakout is reachable through the handle, not just the name.

    What matters is that no injected *markup* survives. The attacker's URL may
    remain as inert text inside the tag — that renders as a nonsense username,
    not a link — so the assertion is on the delimiters, not the substring.
    """
    evil = "x[/name][url=https://evil.example]click[/url]"
    got = ac.render({"name": "N", "handles": {platform: evil}}, platform)
    # The injected tag itself must not survive in a form any renderer would
    # parse. Bracket characters may remain percent-encoded inside a URL.
    for fragment in ("[url=https://evil.example]", "](https://evil.example)"):
        assert fragment not in got, got


def test_parens_in_a_handle_do_not_truncate_the_link():
    """`name_(artist)` is a real e621 handle shape; a bare ) ends a link."""
    artist = {"name": "Brightmoth", "handles": {"e621": "brightmoth_(artist)"}}
    for platform in ("e621", "fn", "ik"):
        got = ac.render(artist, platform)
        assert "(artist)" not in got, got
        assert "%28artist%29" in got, got


@pytest.mark.parametrize("platform", ["fa", "ib", "ws", "e621", "fn", "bsky", "da", "tw"])
def test_newline_in_a_name_cannot_append_lines(platform):
    """A newline would otherwise inject arbitrary extra lines into the post."""
    got = ac.render({"name": "evil\nInjected line", "handles": {}}, platform)
    assert "\n" not in got, repr(got)


def test_sanitising_keeps_the_credit_readable():
    """Stripping must not mangle an ordinary name."""
    assert ac.render({"name": "Nine Tails", "handles": {}}, "fa") == "Art by Nine Tails"
    assert ac.render({"name": "Soy Catate", "handles": {}}, "e621") == "Art by Soy Catate"


# ------------------------------------------------ append_to word boundaries
def test_short_name_inside_a_longer_word_still_gets_credited():
    """A bare substring test silently loses the credit — the module's own
    first rule is that a credit is never dropped."""
    artist = {"name": "art", "handles": {}}
    out = ac.append_to("A lovely artistic piece.", artist, "fa")
    assert out.endswith("Art by art"), out


def test_genuine_existing_mention_still_suppresses():
    artist = {"name": "Inkwolf", "handles": {}}
    desc = "A gift from Inkwolf."
    assert ac.append_to(desc, artist, "fa") == desc


@pytest.mark.parametrize("name,desc", [
    ("Hini", "The machine hums."),
    ("Theoo", "He said the oo sound."),
    ("HIMME", "She hit him metaphorically."),
])
def test_real_short_names_are_not_swallowed(name, desc):
    """Three artists in the catalogue are five characters or fewer."""
    out = ac.append_to(desc, {"name": name, "handles": {}}, "fa")
    assert out != desc


# ------------------------------------------------- through build_artwork_package
import json  # noqa: E402

from posting import artwork_reader as ar  # noqa: E402


@pytest.fixture
def piece(tmp_path, monkeypatch):
    """An artwork folder with an artist and a flat legacy tag list."""
    d = tmp_path / "Piece"
    d.mkdir()
    (d / "img.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (d / "masterpiece.json").write_text(json.dumps({
        "title": "A Piece", "image": "img.png", "rating": "adult",
        "description": "A blurb about the picture.",
        "tags": {"default": ["tiger", "solo"]},
        "artist": {"name": "Inkwolf", "handles": {"fa": "inkwolf"}},
    }), encoding="utf-8")
    monkeypatch.setattr(ar, "get_artwork_archive_path", lambda: tmp_path)
    monkeypatch.setattr("posting.attribution.maybe_append", lambda desc, plat: desc)
    return ar.load_artwork("Piece")


def test_package_description_carries_the_native_credit(piece):
    pkg = ar.build_artwork_package(piece, "fa")
    assert pkg.description == "A blurb about the picture.\n\nArt by :iconinkwolf:"


def test_package_credit_differs_per_platform(piece):
    assert ar.build_artwork_package(piece, "ib").description.endswith(
        "Art by [fa]inkwolf[/fa]")
    assert ar.build_artwork_package(piece, "bsky").description.endswith(
        "Art by Inkwolf")


def test_artist_tag_prepended_on_booru_platforms(piece):
    """Prepended, not appended — budgets trim from the tail."""
    assert ar.build_artwork_package(piece, "e621").tags[0] == "inkwolf"
    assert ar.build_artwork_package(piece, "ib").tags[0] == "inkwolf"


def test_artist_tag_absent_on_gallery_platforms(piece):
    """On FA the credit belongs in the description, not as a name tag."""
    assert "inkwolf" not in ar.build_artwork_package(piece, "fa").tags


def test_artist_tag_not_duplicated_when_already_present(tmp_path, monkeypatch):
    d = tmp_path / "P2"
    d.mkdir()
    (d / "img.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (d / "masterpiece.json").write_text(json.dumps({
        "title": "P2", "image": "img.png", "rating": "adult", "description": "x",
        "tags": {"default": ["Inkwolf", "tiger"]},
        "artist": {"name": "Inkwolf", "handles": {}},
    }), encoding="utf-8")
    monkeypatch.setattr(ar, "get_artwork_archive_path", lambda: tmp_path)
    monkeypatch.setattr("posting.attribution.maybe_append", lambda desc, plat: desc)
    tags = ar.build_artwork_package(ar.load_artwork("P2"), "e621").tags
    assert [t.lower() for t in tags].count("inkwolf") == 1


def test_explicit_tags_override_is_left_alone(piece):
    """The UI saying 'post exactly these' must not be edited behind the scenes."""
    pkg = ar.build_artwork_package(piece, "e621", tags_override=["only", "these"])
    assert pkg.tags == ["only", "these"]


def test_artwork_without_an_artist_is_unchanged(tmp_path, monkeypatch):
    d = tmp_path / "P3"
    d.mkdir()
    (d / "img.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (d / "masterpiece.json").write_text(json.dumps({
        "title": "P3", "image": "img.png", "rating": "adult",
        "description": "Just a blurb.", "tags": {"default": ["tiger"]},
    }), encoding="utf-8")
    monkeypatch.setattr(ar, "get_artwork_archive_path", lambda: tmp_path)
    monkeypatch.setattr("posting.attribution.maybe_append", lambda desc, plat: desc)
    pkg = ar.build_artwork_package(ar.load_artwork("P3"), "e621")
    assert pkg.description == "Just a blurb."
    assert pkg.tags == ["tiger"]
