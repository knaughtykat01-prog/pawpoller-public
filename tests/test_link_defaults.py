"""Link behaviour as a posting-settings default (LINKDEF, 4.35.0).

ANNOUNCEDEF (4.34.0) gave the announcers' yes/no options a middle rung: a per-piece
value wins, the operator's setting decides what "Default" MEANS, and the built-in
answers when nothing is configured. It covered spoiler, tags, caption, silent,
protect, document and pin.

It did not cover the one that decides **what leaves the site** — whether a post
carries a link out to where the work actually lives, and which link. That stayed a
constant in the resolver, so "never link off-site from X" had to be set on every
piece, one at a time, for ever. Exactly the complaint ANNOUNCEDEF existed to fix,
left open on the option with the most reach.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from posting import announce

EXISTING = [("fa", "https://example.com/fa/1"), ("ws", "https://example.com/ws/2")]


def _pkg(extra):
    """resolve_links reads nothing but package.extra."""
    return SimpleNamespace(extra=extra)


def _settings(platform, **opts):
    return {"announce_defaults": {platform: opts}}


class TestTheDefaultDecidesWhenThePieceHasNotChosen:

    def test_no_setting_keeps_the_old_behaviour(self):
        """A no-op on an install that never opened the setting. `auto` carries every
        EXISTING link (it falls back to this publish's first only when there are
        none yet), so the baseline here is both."""
        got = announce.resolve_links(_pkg({"links_by_platform": EXISTING}), {}, "tw")
        assert got == [u for _, u in EXISTING]

    def test_a_default_of_none_stops_linking_off_site(self):
        """The actual ask: turn it off once, not per piece."""
        got = announce.resolve_links(
            _pkg({"links_by_platform": EXISTING}), _settings("tw", link_mode="none"), "tw")
        assert got == []

    def test_a_default_of_all_carries_every_link(self):
        got = announce.resolve_links(
            _pkg({"links_by_platform": EXISTING}), _settings("bsky", link_mode="all"), "bsky")
        assert len(got) == 2

    def test_the_default_is_per_platform(self):
        """Telegram announcing to your own channel and X posting publicly are not the
        same decision, so they do not share one switch."""
        s = {"announce_defaults": {"tw": {"link_mode": "none"}, "tg": {"link_mode": "all"}}}
        assert announce.resolve_links(_pkg({"links_by_platform": EXISTING}), s, "tw") == []
        assert len(announce.resolve_links(_pkg({"links_by_platform": EXISTING}), s, "tg")) == 2

    def test_a_default_order_is_honoured(self):
        got = announce.resolve_links(
            _pkg({"links_by_platform": EXISTING}),
            _settings("tg", link_mode="pick", link_platforms=["ws"]), "tg")
        assert got == ["https://example.com/ws/2"]


class TestThePieceStillWins:
    """The top rung. A default that overrode a deliberate per-piece choice would be
    worse than no default at all."""

    def test_a_piece_can_link_where_the_default_says_not_to(self):
        got = announce.resolve_links(
            _pkg({"links_by_platform": EXISTING, "link_mode": "all"}),
            _settings("tw", link_mode="none"), "tw")
        assert len(got) == 2

    def test_a_piece_can_stay_silent_where_the_default_links(self):
        got = announce.resolve_links(
            _pkg({"links_by_platform": EXISTING, "link_mode": "none"}),
            _settings("tw", link_mode="all"), "tw")
        assert got == []

    def test_a_piece_order_beats_the_default_order(self):
        got = announce.resolve_links(
            _pkg({"links_by_platform": EXISTING, "link_mode": "pick",
                  "link_platforms": ["fa"]}),
            _settings("tw", link_mode="pick", link_platforms=["ws"]), "tw")
        assert got == ["https://example.com/fa/1"]


class TestItCannotSilentlyStartLinking:
    """A malformed mode reading as "auto" would quietly begin posting links off-site
    from a piece whose owner had said not to — the failure that matters here is the
    one that leaks, not the one that refuses."""

    def test_an_unknown_mode_behaves_exactly_like_no_setting(self):
        """Not "like some mode" -- like the built-in, so a typo in the stored
        setting cannot quietly change what goes out."""
        baseline = announce.resolve_links(_pkg({"links_by_platform": EXISTING}), {}, "tw")
        got = announce.resolve_links(
            _pkg({"links_by_platform": EXISTING}), _settings("tw", link_mode="nonsense"), "tw")
        assert got == baseline

    def test_the_route_refuses_a_mode_outside_the_list(self):
        src = open("routes/api.py", encoding="utf-8").read()
        i = src.index('if k == "link_mode":')
        assert "LINK_MODES" in src[i:i + 300], "the whitelist must be the real one"

    def test_the_route_keeps_the_picked_order(self):
        """`pick` reads the list as the ORDER links appear in, so sorting or
        set()-ing it would silently reorder live posts."""
        src = open("routes/api.py", encoding="utf-8").read()
        i = src.index('if k == "link_platforms":')
        block = src[i:i + 700]
        assert "order kept" in block
        assert "sorted(" not in block

    def test_no_platform_means_no_default_lookup(self):
        """resolve_links is called from places with no platform in hand (the linked-mode
        announcer builds its own). Those must not accidentally inherit tw's default."""
        got = announce.resolve_links(
            _pkg({"links_by_platform": EXISTING}), _settings("tw", link_mode="none"))
        assert got == [u for _, u in EXISTING], "unscoped calls keep the built-in"


class TestTheAnnouncersActuallyPassTheirSettings:
    """The plumbing. A default nothing reads is not a default."""

    @pytest.mark.parametrize("path,code", [
        ("posting/platforms/twitter.py", '"tw"'),
        ("posting/platforms/bluesky.py", '"bsky"'),
        ("posting/platforms/telegram.py", '"tg"'),
    ])
    def test_each_announcer_scopes_its_lookup(self, path, code):
        src = open(path, encoding="utf-8").read()
        assert f"platform={code}" in src or f", {code})" in src, \
            f"{path} does not tell resolve_links which platform it is"
        assert "settings" in src

    def test_option_value_is_a_no_op_when_unconfigured(self):
        assert announce.option_value({}, "tw", "link_mode", "auto") == "auto"
        assert announce.option_value(None, "tw", "link_mode", "auto") == "auto"
        assert announce.option_value(
            {"announce_defaults": {"tw": {}}}, "tw", "link_mode", "auto") == "auto"

    def test_an_empty_string_is_not_a_choice(self):
        """A <select> sends '' for its blank option; that means unset, not a mode."""
        assert announce.option_value(
            {"announce_defaults": {"tw": {"link_mode": ""}}}, "tw", "link_mode", "auto") == "auto"


class TestTheSettingsControlExists:
    """A default with no way to set it is an API, not a setting."""

    def test_the_panel_renders_a_mode_for_every_announcer(self):
        src = open("frontend/js/app.js", encoding="utf-8").read()
        assert "_linkDefaultRow(code" in src
        assert "link_mode" in src and "link_platforms" in src

    def test_every_mode_is_offered(self):
        from posting.announce import LINK_MODES
        src = open("frontend/js/app.js", encoding="utf-8").read()
        i = src.index("LINK_MODE_OPTS")
        block = src[i:i + 900]
        for mode in LINK_MODES:
            assert f'"{mode}"' in block, f"{mode} is not offered in the UI"

    def test_blank_stores_nothing(self):
        """"Built-in" has to stay a real third state — storing "" would make the
        backend read an empty string as a choice."""
        src = open("frontend/js/app.js", encoding="utf-8").read()
        # the SAVE handler, not the render above it
        i = src.index('querySelectorAll("[data-anndef-val]")')
        block = src[i:i + 700]
        assert "if (!raw) return;" in block

    def test_the_order_field_is_split_into_a_list(self):
        src = open("frontend/js/app.js", encoding="utf-8").read()
        # the SAVE handler, not the render above it
        i = src.index('querySelectorAll("[data-anndef-val]")')
        assert 'raw.split(",")' in src[i:i + 700]

    def test_the_order_field_only_shows_for_pick(self):
        """It means nothing in the other four modes, and a box that is ignored most
        of the time is worse than no box."""
        src = open("frontend/js/app.js", encoding="utf-8").read()
        assert 'data-anndef-pick' in src
        assert 'sel.value === "pick"' in src

    def test_toggling_does_not_redraw_the_panel(self):
        """A redraw mid-edit drops focus and loses a half-typed order."""
        src = open("frontend/js/app.js", encoding="utf-8").read()
        i = src.index("data-anndef-pick]")
        assert "row.style.display" in src[i:i + 400]
