"""Discord announcements: the picture and Telegram-style options (4.41.0, spec 008).

The announcement used to be a text card — title, raw platform codes, rating —
because it could only show an image already on the public web and no caller
had one. Now the picture travels WITH the message, and the options follow the
same three rungs as Telegram / X / Bluesky: piece, then setting, then built-in.
The webhook is exercised through httpx.MockTransport; nothing touches Discord.
"""
from __future__ import annotations

import asyncio
import json
import logging

import httpx
import pytest

import config
from posting import discord

HOOK = "https://discord.com/api/webhooks/123/SECRET-TOKEN-VALUE"


def _png(tmp_path, size=(3000, 2000)):
    from PIL import Image
    p = tmp_path / "piece.png"
    Image.new("RGB", size, (200, 120, 40)).save(p)
    return p


class TestOptions:

    def test_built_ins(self):
        o = discord.resolve_options(None, {}, "general")
        assert (o["image"], o["spoiler"], o["silent"], o["caption"], o["tags"]) == (True, False, False, True, False)

    def test_a_setting_changes_what_default_means(self):
        s = {"announce_defaults": {"discord": {"silent": True, "tags": True}}}
        o = discord.resolve_options(None, s, "general")
        assert o["silent"] is True and o["tags"] is True

    def test_the_piece_wins(self):
        s = {"announce_defaults": {"discord": {"image": True}}}
        assert discord.resolve_options({"image": False}, s, "general")["image"] is False

    def test_adult_work_is_blurred_whatever_the_setting_says(self):
        """The floor, like Telegram's spoiler: a setting can't un-blur adult work."""
        s = {"announce_defaults": {"discord": {"spoiler": False}}}
        for rating in ("mature", "adult", "explicit"):
            assert discord.resolve_options(None, s, rating)["spoiler"] is True

    def test_only_the_piece_can_unblur_adult_work(self):
        assert discord.resolve_options({"spoiler": False}, {}, "adult")["spoiler"] is False

    def test_a_setting_can_blur_everything(self):
        s = {"announce_defaults": {"discord": {"spoiler": True}}}
        assert discord.resolve_options(None, s, "general")["spoiler"] is True


class TestMessage:

    def _msg(self, **kw):
        base = dict(rating="general", body="A quiet evening.", tags=["fox", "sunset"],
                    site_links=[("fa", "https://www.furaffinity.net/view/1/"),
                                ("ib", "https://inkbunny.net/s/2")],
                    image=(b"IMG", "webp"))
        base.update(kw)
        return discord.build_message("artwork", "Coffee Run", settings={}, **base)

    def test_the_picture_is_in_the_card(self):
        m = self._msg()
        embed = m["payload"]["embeds"][0]
        assert m["file"] == ("preview.webp", b"IMG")
        assert embed["image"] == {"url": "attachment://preview.webp"}

    def test_a_blurred_picture_is_a_spoiler_beside_the_card(self):
        m = self._msg(rating="adult")
        assert m["file"][0] == "SPOILER_preview.webp"
        assert "image" not in m["payload"]["embeds"][0], "an embed image cannot be spoilered"

    def test_sites_by_name_each_a_link(self):
        embed = self._msg()["payload"]["embeds"][0]
        where = next(f for f in embed["fields"] if f["name"] == "Where")["value"]
        assert "[FurAffinity](https://www.furaffinity.net/view/1/)" in where
        assert "[Inkbunny](https://inkbunny.net/s/2)" in where
        assert "fa," not in where and embed["url"] == "https://www.furaffinity.net/view/1/"

    def test_every_site_by_default_first_only_when_asked(self):
        embed = self._msg(opts=discord.resolve_options({"link_mode": "first"}, {}, "general"))["payload"]["embeds"][0]
        where = next(f for f in embed["fields"] if f["name"] == "Where")["value"]
        assert "Inkbunny" not in where and "FurAffinity" in where

    def test_silent_sets_the_no_ping_flag(self):
        m = self._msg(opts=discord.resolve_options({"silent": True}, {}, "general"))
        assert m["payload"]["flags"] == discord.SUPPRESS_NOTIFICATIONS
        assert "flags" not in self._msg()["payload"]

    def test_description_on_hashtags_off_by_default(self):
        d = self._msg()["payload"]["embeds"][0]["description"]
        assert d == "A quiet evening."

    def test_hashtags_when_asked_and_dropped_first_when_too_long(self):
        with_tags = discord.resolve_options({"tags": True}, {}, "general")
        assert "#fox #sunset" in self._msg(opts=with_tags)["payload"]["embeds"][0]["description"]
        long = "x" * 4090
        d = self._msg(body=long, opts=with_tags)["payload"]["embeds"][0]["description"]
        assert d == long, "the hashtags go before the description is cut"

    def test_no_image_option_or_no_image_is_a_text_card(self):
        assert self._msg(opts=discord.resolve_options({"image": False}, {}, "general"))["file"] is None
        assert self._msg(image=None)["file"] is None


class TestPreview:

    def test_a_big_original_is_shrunk_to_a_webp(self, tmp_path):
        data, ext = discord.preview_bytes(_png(tmp_path))
        from PIL import Image
        import io
        im = Image.open(io.BytesIO(data))
        assert ext == "webp" and max(im.size) <= discord.PREVIEW_MAX

    def test_missing_file_is_no_image(self, tmp_path):
        assert discord.preview_bytes(tmp_path / "nope.png") is None
        assert discord.preview_bytes(None) is None


def _mock(monkeypatch, status=204, seen=None, exc=None):
    def handler(request):
        if exc:
            raise exc
        if seen is not None:
            seen.append(request)
        return httpx.Response(status)
    real = httpx.AsyncClient
    monkeypatch.setattr(discord.httpx, "AsyncClient",
                        lambda *a, **k: real(*a, transport=httpx.MockTransport(handler), **k))


class TestSending:

    def _settings(self, monkeypatch, **extra):
        s = {"discord_webhook_url": HOOK, "discord_announce_on_publish": True, **extra}
        monkeypatch.setattr(config, "get_settings", lambda: s)

    def test_a_picture_goes_as_multipart(self, monkeypatch, tmp_path):
        self._settings(monkeypatch)
        seen = []
        _mock(monkeypatch, seen=seen)
        asyncio.run(discord.announce_publish("artwork", "Coffee Run", rating="general",
                                             image_path=_png(tmp_path),
                                             site_links=[("fa", "https://www.furaffinity.net/view/1/")]))
        body = seen[0].content
        assert b'name="payload_json"' in body and b'filename="preview.webp"' in body
        payload = json.loads(body.split(b'name="payload_json"\r\n\r\n')[1].split(b"\r\n--")[0])
        assert payload["embeds"][0]["image"]["url"] == "attachment://preview.webp"

    def test_the_per_publish_tick_overrides_the_switch(self, monkeypatch):
        self._settings(monkeypatch, discord_announce_on_publish=False)
        seen = []
        _mock(monkeypatch, seen=seen)
        asyncio.run(discord.announce_publish("artwork", "A", rating="general"))
        assert seen == []
        asyncio.run(discord.announce_publish("artwork", "A", rating="general", force=True))
        assert len(seen) == 1
        self._settings(monkeypatch, discord_announce_on_publish=True)
        asyncio.run(discord.announce_publish("artwork", "A", rating="general", force=False))
        assert len(seen) == 1, "unticked for this publish = nothing sent"

    def test_a_failure_never_raises_and_never_logs_the_webhook(self, monkeypatch, caplog):
        self._settings(monkeypatch)
        _mock(monkeypatch, exc=httpx.ConnectError(f"cannot reach {HOOK}"))
        with caplog.at_level(logging.DEBUG):
            asyncio.run(discord.announce_publish("artwork", "A", rating="general"))
        assert "SECRET-TOKEN-VALUE" not in _ours(caplog)

    def test_a_rejection_is_logged_by_status_only(self, monkeypatch, caplog):
        self._settings(monkeypatch)
        _mock(monkeypatch, status=400)
        with caplog.at_level(logging.DEBUG):
            ok = asyncio.run(discord.announce("artwork", "A", rating="general"))
        assert ok is False and "400" in _ours(caplog) and "SECRET-TOKEN-VALUE" not in _ours(caplog)


def _ours(caplog):
    """This module's own log lines. httpx logs every request URL at INFO — the app
    silences that at startup (log_redaction.silence_url_loggers, pinned below);
    this test runs without the startup, so it looks at what THIS code logs."""
    return " | ".join(r.getMessage() for r in caplog.records if r.name.startswith("posting.discord"))


def test_the_app_silences_httpx_url_logging():
    """Without this, httpx would log the webhook URL (the secret) on every announce."""
    src = open("log_redaction.py", encoding="utf-8").read()
    assert "httpx" in src and "silence_url_loggers()" in src


class TestManualAnnounceRoute:
    """POST /api/discord/announce — the blur follows the named piece's own rating."""

    def _client(self, monkeypatch, tmp_path, seen):
        from types import SimpleNamespace
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from posting import artwork_reader, manager
        from routes import discord_api
        monkeypatch.setattr(config, "get_settings", lambda: {"discord_webhook_url": HOOK})
        _mock(monkeypatch, seen=seen)
        img = _png(tmp_path, size=(40, 30))
        monkeypatch.setattr(artwork_reader, "load_artwork",
                            lambda name: SimpleNamespace(rating="adult", categories_by_platform={}))
        monkeypatch.setattr(manager, "_discord_preview_source", lambda art: img)
        app = FastAPI()
        app.include_router(next(v for v in vars(discord_api).values() if type(v).__name__ == "APIRouter"))
        return TestClient(app)

    def test_named_adult_piece_is_blurred_even_without_a_rating(self, monkeypatch, tmp_path):
        seen = []
        c = self._client(monkeypatch, tmp_path, seen)
        r = c.post("/api/discord/announce", json={"title": "x", "artwork_name": "Some_Piece", "rating": "general"})
        assert r.status_code == 200
        assert b'filename="SPOILER_preview.webp"' in seen[0].content

    def test_bad_site_links_are_a_400(self, monkeypatch, tmp_path):
        seen = []
        c = self._client(monkeypatch, tmp_path, seen)
        assert c.post("/api/discord/announce", json={"title": "x", "site_links": ["oops"]}).status_code == 400
        assert seen == []
