"""Instagram from any desktop — the image-host ladder (4.7.0).

Meta fetches a post's image from a public URL; a desktop has none. A publish
climbs: this instance's public base → a paired server → the PawPoller relay →
a temporary tunnel. These tests pin the order, the fallbacks, the open relay's
guards, the tunnel helper's checksum pin, and the tiny local image server.
No network: every HTTP call goes through a mock transport.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import os
import tempfile

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

import config
from posting import ig_host, ig_media, ig_tunnel


@pytest.fixture()
def image(tmp_path):
    p = tmp_path / "piece.png"
    Image.new("RGB", (64, 48), (200, 90, 30)).save(p)
    return str(p)


@pytest.fixture()
def stash_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    (tmp_path / "data").mkdir()
    return tmp_path / "data" / "ig_pending"


# ── the ladder ───────────────────────────────────────────────────────────────

class TestLadder:
    def test_a_public_base_wins_and_cleans_its_stash(self, image, stash_dir):
        hosted = asyncio.run(ig_host.host_images([image], {"ig_public_base_url": "https://me.example"}))
        assert hosted.how == "local" and hosted.urls[0].startswith("https://me.example/api/ig/pubmedia/")
        assert len(list(stash_dir.glob("*.jpg"))) == 1
        asyncio.run(hosted.close())
        assert not list(stash_dir.glob("*.jpg"))

    def test_paired_server_beats_the_relay(self, image, monkeypatch):
        seen = []

        async def up(endpoint, path, api_key="", http=None):
            seen.append((endpoint, api_key))
            return "https://srv/api/ig/pubmedia/aa.jpg"
        monkeypatch.setattr(ig_host, "upload_to_host", up)
        hosted = asyncio.run(ig_host.host_images([image], {"posting_server_url": "https://srv/",
                                                            "posting_server_api_key": "pp_k"}))
        assert hosted.how == "paired" and seen == [("https://srv/api/ig/pubmedia", "pp_k")]

    def test_a_dead_paired_server_falls_through_to_the_relay(self, image, monkeypatch):
        calls = []

        async def up(endpoint, path, api_key="", http=None):
            calls.append(endpoint)
            if "pubmedia" in endpoint:
                raise RuntimeError("could not reach srv: ConnectError")
            return "https://pawpoller.example/api/ig/pubmedia/bb.jpg"
        monkeypatch.setattr(ig_host, "upload_to_host", up)
        hosted = asyncio.run(ig_host.host_images([image], {"posting_server_url": "https://srv"}))
        assert hosted.how == "relay" and calls[-1] == config.IG_RELAY_DEFAULT_URL

    def test_relay_url_can_be_pointed_elsewhere(self, image, monkeypatch):
        calls = []

        async def up(endpoint, path, api_key="", http=None):
            calls.append(endpoint)
            return "https://other/x.jpg"
        monkeypatch.setattr(ig_host, "upload_to_host", up)
        asyncio.run(ig_host.host_images([image], {"ig_relay_url": "https://other/api/ig/relay"}))
        assert calls == ["https://other/api/ig/relay"]

    def test_relay_failure_falls_through_to_the_tunnel(self, image, monkeypatch, stash_dir):
        async def up(endpoint, path, api_key="", http=None):
            raise RuntimeError("pawpoller.example answered HTTP 429: Too many images")
        monkeypatch.setattr(ig_host, "upload_to_host", up)
        monkeypatch.setattr(ig_tunnel, "helper_status", lambda: {"supported": True, "present": True})
        closed = []

        class FakeHost:
            base_url = "https://abc-def.trycloudflare.com"

            async def close(self):
                closed.append(True)

        async def open_host():
            return FakeHost()
        monkeypatch.setattr(ig_tunnel, "open_public_host", open_host)
        hosted = asyncio.run(ig_host.host_images([image], {}))
        assert hosted.how == "tunnel"
        assert hosted.urls[0].startswith("https://abc-def.trycloudflare.com/") and hosted.urls[0].endswith(".jpg")
        assert len(list(stash_dir.glob("*.jpg"))) == 1
        asyncio.run(hosted.close())
        assert closed == [True] and not list(stash_dir.glob("*.jpg"))

    def test_nothing_works_says_what_was_tried(self, image, monkeypatch):
        async def up(endpoint, path, api_key="", http=None):
            raise RuntimeError("could not reach pawpoller.example: ConnectError")
        monkeypatch.setattr(ig_host, "upload_to_host", up)
        monkeypatch.setattr(ig_tunnel, "helper_status", lambda: {"supported": True, "present": False})
        with pytest.raises(ig_host.NoPublicHost) as ei:
            asyncio.run(ig_host.host_images([image], {}))
        msg = str(ei.value)
        assert "the PawPoller relay (could not reach" in msg
        assert "helper not downloaded" in msg
        assert "Settings → Posting" in msg

    def test_both_rungs_can_be_switched_off(self, image, monkeypatch):
        called = []

        async def up(*a, **k):
            called.append(1)
        monkeypatch.setattr(ig_host, "upload_to_host", up)
        with pytest.raises(ig_host.NoPublicHost) as ei:
            asyncio.run(ig_host.host_images([image], {"ig_relay_enabled": False, "ig_tunnel_enabled": "off"}))
        assert not called and "turned off" in str(ei.value)


class TestUploadToHost:
    def test_returns_the_hosts_url_and_sends_the_version(self, image):
        seen = {}

        async def h(req):
            seen["auth"] = req.headers.get("authorization")
            seen["ver"] = req.headers.get("x-pawpoller-version")
            seen["path"] = req.url.path
            assert b"piece.png" in req.content
            return httpx.Response(200, json={"url": "https://h/api/ig/pubmedia/cc.jpg"})
        http = httpx.AsyncClient(transport=httpx.MockTransport(h))
        out = asyncio.run(ig_host.upload_to_host("https://h/api/ig/relay", image, http=http))
        assert out == "https://h/api/ig/pubmedia/cc.jpg"
        assert seen == {"auth": None, "ver": config.APP_VERSION, "path": "/api/ig/relay"}

    def test_carries_the_hosts_own_sentence_on_refusal(self, image):
        async def h(req):
            return httpx.Response(403, json={"detail": "This PawPoller server does not host Instagram images for other installs."})
        http = httpx.AsyncClient(transport=httpx.MockTransport(h))
        with pytest.raises(RuntimeError) as ei:
            asyncio.run(ig_host.upload_to_host("https://h/api/ig/relay", image, http=http))
        assert "h answered HTTP 403: This PawPoller server does not host" in str(ei.value)


# ── the open relay route ─────────────────────────────────────────────────────

@pytest.fixture()
def relay_client(monkeypatch, stash_dir):
    from routes import ig_api
    app = FastAPI()
    app.include_router(ig_api.ig_router)
    ig_api._RELAY_HITS.clear()
    state = {"ig_relay_open": True, "ig_public_base_url": "https://pub.example"}
    monkeypatch.setattr(config, "get_settings", lambda: dict(state))
    return TestClient(app), state


def _png_bytes(w=32, h=32):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (1, 2, 3)).save(buf, format="PNG")
    return buf.getvalue()


class TestRelayRoute:
    def test_hosts_an_image_and_answers_with_the_public_url(self, relay_client, stash_dir):
        c, _ = relay_client
        r = c.post("/api/ig/relay", files={"file": ("a.png", _png_bytes(), "image/png")})
        assert r.status_code == 200, r.text
        assert r.json()["url"].startswith("https://pub.example/api/ig/pubmedia/") and r.json()["expires_in"] == 900
        assert len(list(stash_dir.glob("*.jpg"))) == 1
        # …and the public GET serves it as JPEG
        tok = r.json()["url"].rsplit("/", 1)[-1]
        g = c.get(f"/api/ig/pubmedia/{tok}")
        assert g.status_code == 200 and g.content[:3] == b"\xff\xd8\xff"

    def test_refuses_when_not_opted_in(self, relay_client):
        c, state = relay_client
        state["ig_relay_open"] = False
        r = c.post("/api/ig/relay", files={"file": ("a.png", _png_bytes(), "image/png")})
        assert r.status_code == 403 and "does not host" in r.json()["detail"]

    def test_refuses_without_a_public_base(self, relay_client):
        c, state = relay_client
        state["ig_public_base_url"] = ""
        r = c.post("/api/ig/relay", files={"file": ("a.png", _png_bytes(), "image/png")})
        assert r.status_code == 503

    def test_rejects_non_images_and_oversize(self, relay_client, monkeypatch):
        c, _ = relay_client
        r = c.post("/api/ig/relay", files={"file": ("a.txt", b"hello there", "text/plain")})
        assert r.status_code == 400 and r.json()["detail"] == "Not an image"
        monkeypatch.setattr(config, "IG_RELAY_MAX_BYTES", 100)
        r = c.post("/api/ig/relay", files={"file": ("a.png", _png_bytes(400, 400), "image/png")})
        assert r.status_code == 413

    def test_rate_limits_one_address(self, relay_client, monkeypatch):
        c, _ = relay_client
        monkeypatch.setattr(config, "IG_RELAY_PER_IP", (2, 600))
        for _ in range(2):
            assert c.post("/api/ig/relay", files={"file": ("a.png", _png_bytes(), "image/png")}).status_code == 200
        r = c.post("/api/ig/relay", files={"file": ("a.png", _png_bytes(), "image/png")})
        assert r.status_code == 429
        # a different address (first hop of X-Forwarded-For) is its own bucket
        r = c.post("/api/ig/relay", files={"file": ("a.png", _png_bytes(), "image/png")},
                   headers={"X-Forwarded-For": "203.0.113.9, 10.0.0.1"})
        assert r.status_code == 200

    def test_stops_at_the_pending_cap(self, relay_client, monkeypatch):
        c, _ = relay_client
        monkeypatch.setattr(config, "IG_RELAY_MAX_PENDING", 1)
        assert c.post("/api/ig/relay", files={"file": ("a.png", _png_bytes(), "image/png")}).status_code == 200
        r = c.post("/api/ig/relay", files={"file": ("a.png", _png_bytes(), "image/png")})
        assert r.status_code == 503 and "busy" in r.json()["detail"]

    def test_the_route_is_auth_exempt(self):
        src = open("dashboard.py", encoding="utf-8").read()
        assert '"/api/ig/relay",' in src

    def test_host_status_and_settings(self, relay_client, monkeypatch):
        c, state = relay_client
        saved = {}
        monkeypatch.setattr(config, "save_settings", lambda d: (saved.update(d), state.update(d)))
        monkeypatch.setattr(ig_tunnel, "helper_status", lambda: {"supported": True, "present": False, "version": None,
                                                                 "wanted": config.IG_TUNNEL_HELPER_VERSION})
        st = c.get("/api/ig/host-status").json()
        assert st["relay"]["enabled"] is True and st["relay"]["open"] is True and st["tunnel"]["enabled"] is True
        assert st["relay"]["url"] == config.IG_RELAY_DEFAULT_URL
        r = c.post("/api/ig/host-settings", json={"ig_relay_enabled": False, "ig_relay_url": "ftp://nope"})
        assert r.status_code == 400
        r = c.post("/api/ig/host-settings", json={"ig_relay_enabled": False, "ig_relay_open": False})
        assert r.status_code == 200 and saved == {"ig_relay_enabled": False, "ig_relay_open": False}
        assert r.json()["relay"]["enabled"] is False

    def test_relay_open_never_syncs_to_a_desktop(self):
        assert "ig_relay_open" in config.SYNC_EXCLUDE


# ── the tunnel pieces ────────────────────────────────────────────────────────

class TestTunnelHelper:
    def test_parses_the_quick_tunnel_address(self):
        line = ("2026-09-04T10:00:00Z INF +--------------------------------------------------+\n"
                "INF |  https://weird-otter-rain.trycloudflare.com                              |\n")
        assert ig_tunnel.parse_public_url(line) == "https://weird-otter-rain.trycloudflare.com"
        assert ig_tunnel.parse_public_url("INF Requesting new quick Tunnel on trycloudflare.com...") is None

    def test_download_verifies_the_pinned_checksum(self, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "APPDATA_DIR", tmp_path)
        body = b"not really cloudflared"
        monkeypatch.setattr(config, "IG_TUNNEL_HELPER_ASSETS",
                            {(ig_tunnel.platform.system(), ig_tunnel.platform.machine()):
                             ("cloudflared-test", hashlib.sha256(body).hexdigest())})

        async def h(req):
            assert req.url.path.endswith("/" + config.IG_TUNNEL_HELPER_VERSION + "/cloudflared-test")
            return httpx.Response(200, content=body)
        http = httpx.AsyncClient(transport=httpx.MockTransport(h))
        st = asyncio.run(ig_tunnel.download_helper(http=http))
        assert st["present"] and st["version"] == config.IG_TUNNEL_HELPER_VERSION
        assert ig_tunnel.helper_path().read_bytes() == body

    def test_a_mismatched_download_installs_nothing(self, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "APPDATA_DIR", tmp_path)
        monkeypatch.setattr(config, "IG_TUNNEL_HELPER_ASSETS",
                            {(ig_tunnel.platform.system(), ig_tunnel.platform.machine()):
                             ("cloudflared-test", "00" * 32)})

        async def h(req):
            return httpx.Response(200, content=b"tampered")
        http = httpx.AsyncClient(transport=httpx.MockTransport(h))
        with pytest.raises(RuntimeError) as ei:
            asyncio.run(ig_tunnel.download_helper(http=http))
        assert "did not match its pinned checksum" in str(ei.value)
        assert not ig_tunnel.helper_path().exists()
        assert not list(tmp_path.glob("helpers/*.part"))

    def test_unsupported_machine_is_reported_not_crashed(self, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "APPDATA_DIR", tmp_path)
        monkeypatch.setattr(config, "IG_TUNNEL_HELPER_ASSETS", {})
        st = ig_tunnel.helper_status()
        assert st["supported"] is False and st["present"] is False
        with pytest.raises(RuntimeError) as ei:
            asyncio.run(ig_tunnel.download_helper())
        assert "no tunnel helper build" in str(ei.value)

    def test_pinned_assets_look_like_real_pins(self):
        for key, (asset, sha) in config.IG_TUNNEL_HELPER_ASSETS.items():
            assert asset.startswith("cloudflared-") and len(sha) == 64 and int(sha, 16)
        assert ("Windows", "AMD64") in config.IG_TUNNEL_HELPER_ASSETS
        assert ("Linux", "x86_64") in config.IG_TUNNEL_HELPER_ASSETS


class TestStashServer:
    def test_serves_only_stashed_tokens(self, stash_dir, image):
        tok = ig_media.stash_image(image)
        srv = ig_tunnel.StashServer()
        port = srv.start()
        try:
            base = f"http://127.0.0.1:{port}"
            r = httpx.get(f"{base}/{tok}.jpg")
            assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg" and r.content[:3] == b"\xff\xd8\xff"
            assert httpx.head(f"{base}/{tok}.jpg").status_code == 200
            assert httpx.get(f"{base}/__ping").status_code == 204
            assert httpx.get(f"{base}/nope.jpg").status_code == 404
            assert httpx.get(f"{base}/../settings.json").status_code == 404
            assert httpx.get(f"{base}/").status_code == 404
        finally:
            srv.stop()


class TestPostersUseTheLadder:
    def test_both_publish_paths_call_host_images(self):
        art = open("posting/platforms/instagram.py", encoding="utf-8").read()
        pub = open("posting/post_publisher.py", encoding="utf-8").read()
        assert "ig_host.host_images(" in art and "ig_host.host_images(" in pub
        assert "hosted.close()" in art and "hosted.close()" in pub
        for src in (art, pub):
            assert "pair it with your server" not in src

    def test_the_settings_page_has_the_block(self):
        app = open("frontend/js/app.js", encoding="utf-8").read()
        api = open("frontend/js/api.js", encoding="utf-8").read()
        assert 'id="ig-host-accordion"' in app and "API.getIgHostStatus()" in app
        assert "downloadIgTunnelHelper" in api and "/api/ig/tunnel-helper/download" in api
