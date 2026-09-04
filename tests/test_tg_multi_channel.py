"""Telegram as a multi-channel platform.

A channel is an account. That is what makes "several bots across several
channels, and/or one bot across several channels" work without inventing
anything: the bot token is already a per-account credential field, so two
accounts can share a token or hold different ones.

The two bugs these tests exist for were both live, and both would only have
bitten once a second channel existed:

1. **Silent wrong-channel broadcast.** The poster resolved the channel as
   ``creds.get("tg_channel") or settings.get("tg_channel")``. A second account
   whose channel was unset inherited the DEFAULT account's channel and
   broadcast there with no error. ``post_publisher`` never had that fallback,
   so the two publish paths disagreed while a comment claimed otherwise.
2. **``validate()`` ignored the account**, reading flat settings — so account 2
   was validated against account 1's token and channel.

Verified live before these were written: with two accounts configured, the
default posted successfully and the second failed with Telegram's own
"bot is not a member of the channel chat" rather than quietly posting to the
first channel.
"""
from __future__ import annotations

import pytest

from database import accounts as adb


class TestRegistry:
    def test_telegram_is_an_account_platform(self):
        """Without this, POST /api/accounts hard-rejects tg and it never
        appears in the Add-account dropdown."""
        assert "tg" in adb.PLATFORMS
        assert adb.PLATFORM_NAMES.get("tg") == "Telegram"

    def test_the_channel_is_the_handle(self):
        """(platform, handle) is the natural key for desktop<->server sync.
        With no handle a tg row can only be matched on is_default, so a second
        channel added on one machine inserts as a NEW row on the other and the
        two drift. This is the load-bearing entry."""
        assert adb._HANDLE_KEYS.get("tg") == ["tg_channel"]

    def test_credentials_need_the_posting_bot(self):
        """4.8.0: the notification bot is never borrowed for channel posting —
        a digest once landed in a public channel because one bot did both
        jobs. Only the posting bot counts, and a channel is required, because
        without one there is nowhere to post."""
        check = adb.DEFAULT_CRED_CHECKS["tg"]
        assert check({"tg_bot_token": "x", "tg_channel": "@c"}) is True
        assert check({"telegram_bot_token": "x", "tg_channel": "@c"}) is False, "not borrowed (4.8.0)"
        assert check({"tg_bot_token": "x"}) is False, "a channel is mandatory"
        assert check({}) is False

    def test_no_longer_post_only(self):
        """Telegram WAS post-only: genuinely unpollable while its only per-post
        number arrived solely as a pushed update. Capturing reactions into
        tg_submissions gave it a real stats table, so it graduated in 4.0.10 and
        now appears in analytics like any other platform."""
        assert "tg" not in adb.POST_ONLY_PLATFORMS
        from database import platform_metrics as pm
        spec = pm.get("tg")
        assert spec is not None, "a pollable platform needs a metrics entry"
        assert spec.faves == "reactions_count"
        assert spec.views is None, (
            "views must stay None PERMANENTLY — a channel's view count is not in "
            "the Bot API at all, so this is not a gap a later release can fill")


class TestChannelResolution:
    """The poster must resolve ITS OWN account's channel."""

    @staticmethod
    def _code_only(block: str) -> str:
        """Strip comments. The comment above the fix NAMES the anti-pattern, so
        a naive substring search matches the documentation and fails."""
        return chr(10).join(l for l in block.splitlines()
                            if not l.lstrip().startswith("#"))

    def test_no_fallback_to_another_accounts_channel(self):
        """THE bug. A `or settings.get(...)` fallback on the channel means a
        second account with no channel set broadcasts to the default account's
        channel — silently, to real subscribers."""
        src = open("posting/platforms/telegram.py", encoding="utf-8").read()
        i = src.index("async def post(")
        block = self._code_only(src[i:i + 2500])
        assert 'creds.get("tg_channel", "")' in block
        assert 'or settings.get("tg_channel"' not in block, (
            "the channel must NOT fall back to another account's — that is a "
            "silent wrong-channel broadcast")

    def test_the_bot_token_does_not_fall_back_either(self):
        """Until 4.8.0 the BOT could fall back to the notification bot while the
        CHANNEL could not. Both are now strict: the posting bot is its own bot."""
        src = open("posting/platforms/telegram.py", encoding="utf-8").read()
        i = src.index("async def post(")
        block = self._code_only(src[i:i + 2500])
        assert 'settings.get("telegram_bot_token"' not in block

    def test_validate_resolves_the_account(self):
        """Reading flat settings validated account 2 against account 1's
        credentials — passing when it should fail, failing when it should
        pass."""
        src = open("posting/platforms/telegram.py", encoding="utf-8").read()
        i = src.index("def validate(")
        block = src[i:i + 1400]
        assert "_resolve_creds" in block, (
            "validate() must resolve this poster's account, not read flat keys")


class TestOrphanAdoption:
    """Publishing to Telegram auto-created an account before it was a real
    platform — labelled "tg (default)" with an empty handle. The migration must
    adopt that row, not seed beside it."""

    def test_the_migration_adopts_rather_than_seeds(self):
        src = open("database/db.py", encoding="utf-8").read()
        assert "platform = 'tg'" in src, "no tg adoption migration found"
        i = src.index("platform = 'tg'")
        block = src[max(0, i - 2000):i + 2000]
        assert "UPDATE accounts SET" in block, (
            "must UPDATE the existing row — INSERTing a second default is "
            "rejected by idx_accounts_one_default, and create_account silently "
            "downgrades is_default, leaving an account nobody asked for")

    def test_the_handle_is_backfilled(self):
        src = open("database/db.py", encoding="utf-8").read()
        i = src.index("platform = 'tg'")
        block = src[max(0, i - 2000):i + 2500]
        assert "handle = ?" in block
        assert "account_setting_key" in block, (
            "the channel key differs between default and non-default accounts")
