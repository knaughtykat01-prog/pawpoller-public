"""What "Default" means on X, Bluesky and Telegram (backlog ANNOUNCEDEF).

Each of the three announcing platforms has a per-piece options panel whose controls are
tri-state **Default / On / Off**. "Default" used to mean a constant inside the poster's
`_resolve_options`, so "I never want hashtags on X" had to be set on every piece, one at
a time, for ever.

Three rungs now: the piece, then Settings → Publishing defaults, then the built-in. The
third rung is what keeps an install that never opens the page behaving exactly as before,
so "unset" has to stay distinguishable from "off" at every layer.

Two options are deliberately NOT plain three-rung flags. X's `sensitive` and Telegram's
`spoiler` are **floors**: a setting may add them where the rating asks for none, but must
not strip them from work the rating says is adult. An unflagged adult image is how an
account gets restricted rather than how a post gets refused, so a checkbox that could
turn that off would be a footgun rather than a preference.
"""
from __future__ import annotations

import pytest

from posting import announce
from posting.platforms.base import StoryUploadPackage


def _pkg(rating="general", **extra):
    return StoryUploadPackage(
        story_name="Piece", chapter_index=0, chapter_title="", platform="tw",
        title="Piece", description="d", tags=["a"], rating=rating, file_path="",
        file_type="png", extra=dict(extra))


class TestTheResolver:
    def test_nothing_configured_means_the_built_in(self):
        assert announce.option_default({}, "tw", "tags", True) is True
        assert announce.option_default({}, "tw", "tags", False) is False

    def test_a_setting_replaces_the_built_in(self):
        s = {"announce_defaults": {"tw": {"tags": False}}}
        assert announce.option_default(s, "tw", "tags", True) is False

    def test_one_platforms_setting_does_not_leak_to_another(self):
        s = {"announce_defaults": {"tw": {"tags": False}}}
        assert announce.option_default(s, "bsky", "tags", True) is True

    def test_an_unset_key_still_falls_through(self):
        """Unset must not read as False, or configuring one option would silently
        switch every other one off."""
        s = {"announce_defaults": {"tw": {"caption": False}}}
        assert announce.option_default(s, "tw", "tags", True) is True

    def test_the_legacy_telegram_keys_still_work(self):
        """`tg_no_tags`, `tg_silent`, `tg_protect` and `tg_document` predate the nested
        block. Dropping them would silently change the behaviour of an install that set
        them — and `tg_no_tags` is worded as a negative, so it inverts."""
        assert announce.option_default({"tg_no_tags": True}, "tg", "tags", True) is False
        assert announce.option_default({"tg_no_tags": False}, "tg", "tags", True) is True
        assert announce.option_default({"tg_silent": True}, "tg", "silent", False) is True

    def test_the_nested_block_beats_the_legacy_key(self):
        s = {"tg_no_tags": True, "announce_defaults": {"tg": {"tags": True}}}
        assert announce.option_default(s, "tg", "tags", True) is True


class TestXHonoursTheThreeRungs:
    def test_a_setting_changes_what_default_means(self):
        from posting.platforms import twitter
        s = {"announce_defaults": {"tw": {"tags": False, "alt": False}}}
        opts = twitter._resolve_options(_pkg(), s)
        assert opts["tags"] is False and opts["alt"] is False
        assert opts["caption"] is True          # untouched, still the built-in

    def test_the_piece_still_wins(self):
        from posting.platforms import twitter
        s = {"announce_defaults": {"tw": {"tags": False}}}
        assert twitter._resolve_options(_pkg(tags=True), s)["tags"] is True

    def test_nothing_configured_is_exactly_the_old_behaviour(self):
        from posting.platforms import twitter
        opts = twitter._resolve_options(_pkg(), {})
        assert opts == {"sensitive": False, "tags": True, "caption": True, "alt": True}


class TestSensitiveIsAFloorNotASwitch:
    """The one that would be a footgun if it were an ordinary preference."""

    def test_a_setting_cannot_unflag_adult_work(self):
        from posting.platforms import twitter
        s = {"announce_defaults": {"tw": {"sensitive": False}}}
        assert twitter._resolve_options(_pkg(rating="adult"), s)["sensitive"] is True

    def test_a_setting_can_flag_everything(self):
        from posting.platforms import twitter
        s = {"announce_defaults": {"tw": {"sensitive": True}}}
        assert twitter._resolve_options(_pkg(rating="general"), s)["sensitive"] is True

    def test_the_piece_keeps_its_full_override(self):
        """A deliberate act on ONE piece is different from a blanket setting."""
        from posting.platforms import twitter
        assert twitter._resolve_options(_pkg(rating="adult", sensitive=False), {})["sensitive"] is False

    def test_telegrams_spoiler_is_the_same_shape(self):
        from posting.platforms import telegram
        s = {"announce_defaults": {"tg": {"spoiler": False}}}
        assert telegram._resolve_options(_pkg(rating="adult"), s)["spoiler"] is True
        s2 = {"announce_defaults": {"tg": {"spoiler": True}}}
        assert telegram._resolve_options(_pkg(rating="general"), s2)["spoiler"] is True


class TestBlueskyLabel:
    def test_a_configured_label_applies_when_the_rating_asks_for_none(self):
        from posting.platforms import bluesky
        s = {"announce_defaults": {"bsky": {"label": "nudity"}}}
        assert bluesky._resolve_options(_pkg(rating="general"), s)["labels"] == ["nudity"]

    def test_it_never_weakens_the_label_the_rating_earned(self):
        from posting.platforms import bluesky
        s = {"announce_defaults": {"bsky": {"label": "nudity"}}}
        assert bluesky._resolve_options(_pkg(rating="adult"), s)["labels"] == ["sexual"]

    def test_the_piece_can_still_clear_it(self):
        from posting.platforms import bluesky
        s = {"announce_defaults": {"bsky": {"label": "nudity"}}}
        assert bluesky._resolve_options(_pkg(rating="general", label="none"), s)["labels"] is None


class TestTheSettingsRouteValidates:
    """This reaches the posting path, so it is whitelisted rather than stored as sent."""

    def _clean(self, raw):
        import routes.api as api_mod
        import inspect
        src = inspect.getsource(api_mod)
        assert '"announce_defaults" in body' in src
        return src

    def test_the_whitelist_covers_every_option_the_ui_offers(self):
        """A key the UI can set but the route drops would look saved and do nothing."""
        import inspect, routes.api as api_mod
        src = inspect.getsource(api_mod)
        i = src.index('if "announce_defaults" in body:')
        block = src[i:i + 1400]
        ui = open("frontend/js/app.js", encoding="utf-8").read()
        j = ui.index("ANNOUNCE_DEFAULTS: {")
        ui_block = ui[j:ui.index("SETTINGS_PAGES", j)]
        for key in ("tags", "caption", "alt", "sensitive", "preview", "silent",
                    "protect", "document", "pin", "spoiler"):
            if f"'{key}'" in ui_block:
                assert f'"{key}"' in block, f"the route drops {key}, which the UI offers"

    def test_it_is_returned_to_the_ui(self):
        import inspect, routes.api as api_mod
        assert '"announce_defaults": settings.get("announce_defaults", {})' \
            in inspect.getsource(api_mod)


def test_the_ui_table_matches_each_resolver():
    """The panel's option list and the poster's resolver must not drift — a control with
    no resolver reads as a setting that does nothing."""
    import inspect
    from posting.platforms import twitter, bluesky, telegram

    ui = open("frontend/js/app.js", encoding="utf-8").read()
    i = ui.index("ANNOUNCE_DEFAULTS: {")
    block = ui[i:ui.index("SETTINGS_PAGES", i)]
    for code, mod in (("tw", twitter), ("bsky", bluesky), ("tg", telegram)):
        seg = block[block.index(f"{code}: {{"):]
        seg = seg[:seg.index("] }")]
        src = inspect.getsource(mod._resolve_options)
        for line in seg.splitlines():
            line = line.strip()
            if not line.startswith("['"):
                continue
            key = line.split("'")[1]
            assert f'"{key}"' in src, f"{code}.{key} has no resolver in {mod.__name__}"
