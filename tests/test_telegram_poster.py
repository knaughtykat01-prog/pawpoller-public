"""TelegramPoster — artwork and story announcements to a channel.

Telegram could previously only be reached from the Posts module (2.198.0),
which left PawPoller able to publish a piece to nine sites with no way to tell
the channel about it.

Two things this poster does differently from its neighbours, both pinned here
because both look like mistakes until you know why:

1. ``supports_edit = False`` even though the Bot API *has* edit methods. They
   refuse any message older than 48 hours, so declaring edit support would make
   Masterpiece sync attempt an edit on an old post, fail, and stamp
   ``status='failed'`` on a live, correct message — the exact failure
   ``supports_artwork_edit`` exists to prevent.
2. No 30-hashtag cap. That is Instagram's rule; Telegram has none, and copying
   it would silently drop tags. The real ceiling is caption length, which
   ``validate()`` REPORTS rather than truncating — a broadcast goes to real
   subscribers, so mangling it silently is worse than refusing it.

Network is faked here. The live path was exercised against a real bot and a
real private channel during development: story announcement, artwork, and an
NSFW spoiler-blurred post all landed, and every failure branch was walked.
"""
from __future__ import annotations

import pytest

from posting.platforms.base import StoryUploadPackage
from posting.platforms.telegram import (
    CAPTION_LIMIT,
    MESSAGE_LIMIT,
    TelegramPoster,
    _build_caption,
    _hashtags,
)


def pkg(**kw) -> StoryUploadPackage:
    base = dict(story_name="", chapter_index=0, chapter_title="", platform="tg",
                title="T", description="D", tags=[], rating="general",
                file_path=None, file_type="")
    base.update(kw)
    return StoryUploadPackage(**base)


class TestDeclarations:
    def test_post_only_despite_the_api_having_an_edit(self):
        """The 48-hour window makes edit support a liability, not a feature."""
        p = TelegramPoster()
        assert p.supports_edit is False
        assert p.supports_file_replace is False

    def test_requires_mode_is_any(self):
        """api.telegram.org is reachable from the server. Declaring 'desktop'
        would strand jobs in the queue — see base.py's FurAffinity warning."""
        assert TelegramPoster().requires_mode == "any"

    def test_registered_in_the_poster_registry(self):
        from posting.manager import _get_poster
        assert type(_get_poster("tg")).__name__ == "TelegramPoster"

    def test_offered_as_an_artwork_target_in_the_ui(self):
        """A poster nothing can select is invisible. The picker and the
        registry must agree, and they live in different languages."""
        js = open("frontend/js/artwork.js", encoding="utf-8").read()
        line = next(l for l in js.splitlines() if "_PLATFORMS:" in l)
        assert "'tg'" in line, "Telegram missing from the artwork picker"

    def test_marked_post_only_for_masterpiece_sync(self):
        """Sync must SKIP tg, not attempt an edit that would fail and record a
        healthy post as failed."""
        js = open("frontend/js/masterpieces.js", encoding="utf-8").read()
        line = next(l for l in js.splitlines() if "_POST_ONLY:" in l)
        assert "'tg'" in line

    def test_included_in_the_story_tag_cascade(self):
        """A platform missing from story_reader's list gets NO tags at all —
        the default cascade skips it silently."""
        src = open("posting/story_reader.py", encoding="utf-8").read()
        line = next(l for l in src.splitlines() if "all_poster_ids = [" in l)
        assert '"tg"' in line


class TestCaptions:
    def test_artwork_uses_description_then_hashtags(self):
        out = _build_caption(pkg(description="A quiet piece.", tags=["sunset"]),
                             has_image=True, is_art=True)
        assert out == "A quiet piece.\n\n#sunset"

    def test_artwork_falls_back_to_the_title(self):
        out = _build_caption(pkg(title="Sunset", description=""),
                             has_image=True, is_art=True)
        assert out == "Sunset"

    def test_story_announcement_carries_title_blurb_and_links(self):
        out = _build_caption(
            pkg(story_name="Chosen", chapter_title="Chosen - Ch1",
                description="Chapter one is up.", tags=["mm"],
                extra={"links": ["https://ao3.org/works/1"]}),
            has_image=False, is_art=False)
        assert "Chosen - Ch1" in out
        assert "Chapter one is up." in out
        assert "https://ao3.org/works/1" in out
        assert "#mm" in out

    def test_a_story_body_is_never_included(self):
        """Telegram caps a message at 4,096 characters — about 700 words
        against stories that run to 70,000. A channel announces work, it does
        not host it."""
        out = _build_caption(pkg(story_name="Chosen", description="blurb"),
                             has_image=False, is_art=False)
        assert len(out) < MESSAGE_LIMIT

    def test_hashtags_dedupe_and_keep_order(self):
        assert _hashtags(["sunset", "Sunset", "cat"]) == "#sunset #cat"

    def test_no_instagram_hashtag_cap(self):
        """Instagram stops at 30. Telegram has no such rule, and inheriting it
        would silently drop a user's tags."""
        assert _hashtags([f"t{i}" for i in range(40)]).count("#") == 40


class TestValidate:
    def test_reports_an_over_long_caption_rather_than_truncating(self):
        errs = TelegramPoster().validate(
            # file_type must be an IMAGE type or this takes the text path,
            # where the limit is 4x higher and 1,074 chars is legal.
            pkg(file_path=__file__, file_type="png",
                description="x" * (CAPTION_LIMIT + 50)))
        assert any(str(CAPTION_LIMIT) in e for e in errs), errs

    def test_a_long_text_post_is_allowed_up_to_the_message_limit(self):
        """The caption cap is a QUARTER of the message cap. Applying the
        stricter one to a text-only post would reject valid content."""
        errs = TelegramPoster().validate(pkg(description="x" * (CAPTION_LIMIT + 50)))
        assert not [e for e in errs if "limit" in e], errs

    def test_missing_image_is_caught(self):
        errs = TelegramPoster().validate(pkg(file_path="nope.png", file_type="png"))
        assert any("not found" in e.lower() for e in errs)

    def test_an_empty_post_is_refused(self):
        errs = TelegramPoster().validate(pkg(title="", description=""))
        assert any("nothing to post" in e.lower() for e in errs)


class TestFailuresAreReported:
    @pytest.mark.asyncio
    async def test_an_invite_link_returns_a_result_not_an_exception(self, monkeypatch):
        """TgClient refuses invite links in __init__. A raise escaping post()
        would surface as a crash rather than a failed job."""
        import config
        p = TelegramPoster()
        monkeypatch.setattr(p, "_resolve_creds", lambda *a, **k: {
            "tg_bot_token": "tok", "tg_channel": "https://t.me/+AbCdEf"})
        monkeypatch.setattr(config, "get_settings", lambda: {
            "tg_bot_token": "tok", "tg_channel": "https://t.me/+AbCdEf"})
        res = await p.post(pkg(description="hi"))
        assert res.success is False
        assert "invite" in (res.error or "").lower()

    @pytest.mark.asyncio
    async def test_a_refusal_reports_telegrams_own_reason(self, monkeypatch):
        """The point of 4.0.3: report what Telegram said, not a guess. Verified
        live — a wrong channel returns 'bot is not a member of the channel
        chat', which is what the user needs to see."""
        import config

        class FakeClient:
            def __init__(self, **kw):
                self.channel = "@x"
                self.last_error = "Forbidden: bot is not a member of the channel chat"

            async def create_post(self, *a, **k):
                return None

        monkeypatch.setattr("clients.tg.client.TgClient", FakeClient)
        p = TelegramPoster()
        monkeypatch.setattr(p, "_resolve_creds", lambda *a, **k: {
            "tg_bot_token": "tok", "tg_channel": "@x"})
        monkeypatch.setattr(config, "get_settings", lambda: {
            "tg_bot_token": "tok", "tg_channel": "@x"})
        res = await p.post(pkg(description="hi"))
        assert res.success is False
        assert "not a member" in res.error
