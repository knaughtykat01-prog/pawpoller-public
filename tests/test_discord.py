"""Discord announce webhook (gap G4).

Covers the embed builder's field/colour/thumbnail rules and the auto-announce
gating (off by default, and adult content never carries an inline image). No
real HTTP — discord._send is monkeypatched.
"""
import asyncio

import config
from posting import discord


def test_build_embed_fields_and_colour():
    e = discord.build_embed("artwork", "My Piece", url="https://x/1",
                            rating="general", platforms=["fa", "bsky"])
    assert e["title"] == "My Piece"
    assert e["url"] == "https://x/1"
    assert e["color"] == discord._RATING_COLOR["general"]
    names = {f["name"] for f in e["fields"]}
    assert {"Where", "Rating"} <= names


def test_build_embed_only_attaches_public_thumbnail():
    # A local path is never sent to Discord.
    assert "thumbnail" not in discord.build_embed("artwork", "P", thumbnail="/data/x.png")
    e = discord.build_embed("artwork", "P", thumbnail="https://cdn/x.png")
    assert e["thumbnail"]["url"] == "https://cdn/x.png"


def test_is_configured():
    config.save_settings({"discord_webhook_url": ""})
    assert not discord.is_configured()
    config.save_settings({"discord_webhook_url": "https://discord.com/api/webhooks/1/abc"})
    assert discord.is_configured()


def test_announce_publish_gated_on_toggle(monkeypatch):
    calls = []

    async def _fake_send(url, embed, content=""):
        calls.append(embed)
        return True

    monkeypatch.setattr(discord, "_send", _fake_send)

    # Configured but auto-announce OFF → nothing sent.
    config.save_settings({"discord_webhook_url": "https://discord.com/api/webhooks/1/abc",
                          "discord_announce_on_publish": False})
    asyncio.run(discord.announce_publish("post", "hi", rating="general"))
    assert calls == []

    # Toggle on → one send.
    config.save_settings({"discord_announce_on_publish": True})
    asyncio.run(discord.announce_publish("post", "hi", rating="general"))
    assert len(calls) == 1


def test_announce_publish_drops_adult_thumbnail(monkeypatch):
    sent = {}

    async def _fake_send(url, embed, content=""):
        sent["embed"] = embed
        return True

    monkeypatch.setattr(discord, "_send", _fake_send)
    config.save_settings({"discord_webhook_url": "https://discord.com/api/webhooks/1/abc",
                          "discord_announce_on_publish": True})
    asyncio.run(discord.announce_publish("artwork", "P", rating="adult",
                                         thumbnail="https://cdn/x.png"))
    assert "thumbnail" not in sent["embed"]   # adult → link only, no inline preview
