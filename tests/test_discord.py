"""Discord announce webhook (gap G4) — configuration.

The message itself (picture, blur, options, links, sending) is covered by
tests/test_discord_announce.py since 4.41.0 (spec 008). The tests that used to
live here pinned the OLD behaviour that spec replaced on purpose: an image only
when it was already a public URL, and adult work announced with no image at all
(it is now sent blurred behind Discord's spoiler).
"""
import config
from posting import discord


def test_is_configured():
    config.save_settings({"discord_webhook_url": ""})
    assert not discord.is_configured()
    config.save_settings({"discord_webhook_url": "https://discord.com/api/webhooks/1/abc"})
    assert discord.is_configured()
