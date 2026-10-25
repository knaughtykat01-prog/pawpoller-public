"""The Instagram relay on its own (spec 048).

The relay runs on a public VM where a full PawPoller once double-polled the
operator's accounts. These pin that the relay-only app answers its three paths
with the relay's existing guards, and that nothing else can run there: no
dashboard, no app modules loaded, no start on a real data folder, and no
`update.sh` on a relay-only machine.
"""
from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

import config
import relay_server
from posting import ig_media
from routes import ig_api

ROOT = Path(__file__).resolve().parent.parent
_GIT_BASH = Path(r"C:\Program Files\Gitinash.exe")
BASH = str(_GIT_BASH) if _GIT_BASH.is_file() else shutil.which("bash")
if BASH and "system32" in BASH.lower():
    BASH = None   # WSL's launcher: different paths, maybe no distro at all


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), (200, 90, 30)).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture()
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "get_settings",
                        lambda: {"ig_relay_open": True, "ig_public_base_url": "https://relay.example"})
    monkeypatch.delenv("PAWPOLLER_HOSTED", raising=False)
    ig_api._RELAY_HITS.clear()
    return TestClient(relay_server.build_app())


def _post(c, data=None):
    return c.post("/api/ig/relay", files={"file": ("p.png", data or _png(), "application/octet-stream")})


class TestRelayPaths:
    def test_a_picture_is_hosted_and_served_as_jpeg(self, client):
        r = _post(client)
        assert r.status_code == 200, r.text
        url = r.json()["url"]
        assert url.startswith("https://relay.example/api/ig/pubmedia/") and url.endswith(".jpg")
        got = client.get(url.replace("https://relay.example", ""))
        assert got.status_code == 200 and got.headers["content-type"] == "image/jpeg"

    def test_an_expired_picture_is_gone(self, client, tmp_path):
        url = _post(client).json()["url"]
        f = next((tmp_path / "ig_pending").glob("*.jpg"))
        old = time.time() - ig_media.TTL_SECONDS - 1
        os.utime(f, (old, old))
        assert client.get(url.replace("https://relay.example", "")).status_code == 404
        assert not f.exists()

    def test_video_is_refused(self, client):
        mp4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64
        assert _post(client, mp4).status_code == 415

    def test_the_eleventh_upload_in_ten_minutes_is_refused(self, client):
        for _ in range(config.IG_RELAY_PER_IP[0]):
            assert _post(client).status_code == 200
        assert _post(client).status_code == 429

    def test_responses_say_nosniff(self, client):
        assert client.get("/api/health").headers["x-content-type-options"] == "nosniff"

    def test_an_address_is_forgotten_after_its_window_without_another_upload(self, client):
        _post(client)
        assert ig_api._RELAY_HITS
        ig_api.forget_old_addresses(time.time() + config.IG_RELAY_PER_IP[1] + 1)
        assert not ig_api._RELAY_HITS

    def test_health_says_relay(self, client):
        assert client.get("/api/health").json() == {"status": "ok", "mode": "relay",
                                                    "version": config.APP_VERSION}

    @pytest.mark.parametrize("path", ["/", "/api/settings", "/api/poll/paused", "/docs", "/openapi.json",
                                      "/api/ig/host-status"])
    def test_nothing_else_answers(self, client, path):
        assert client.get(path).status_code == 404


class TestStartRefusals:
    @pytest.fixture(autouse=True)
    def _env(self, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "DB_PATH", tmp_path / "none.db")
        monkeypatch.setattr(config, "_vault_unreadable", False)
        monkeypatch.setenv("IG_PUBLIC_BASE_URL", "https://relay.example")
        monkeypatch.delenv("PAWPOLLER_HOSTED", raising=False)
        monkeypatch.setattr(config, "get_settings", lambda: {})

    def test_clean_start_is_allowed(self):
        assert relay_server.refusal() is None

    @pytest.mark.parametrize("url", ["", "http://relay.example"])
    def test_needs_an_https_address(self, monkeypatch, url):
        monkeypatch.setenv("IG_PUBLIC_BASE_URL", url)
        assert "IG_PUBLIC_BASE_URL" in relay_server.refusal()

    def test_a_hosted_seat_is_never_a_relay(self, monkeypatch):
        monkeypatch.setenv("PAWPOLLER_HOSTED", "1")
        assert "hosted seat" in relay_server.refusal()

    def test_a_data_folder_with_credentials_is_refused_by_name_not_value(self, monkeypatch):
        field = sorted(config.CREDENTIAL_FIELDS)[0]
        monkeypatch.setattr(config, "get_settings", lambda: {field: "s3cret-value"})
        why = relay_server.refusal()
        assert field in why and "s3cret-value" not in why

    def test_a_real_apps_data_folder_is_refused(self, monkeypatch, tmp_path):
        db = tmp_path / "pawpoller.db"
        db.write_bytes(b"")
        monkeypatch.setattr(config, "DB_PATH", db)
        assert "belongs to a PawPoller app" in relay_server.refusal()

    def test_a_vault_it_cant_open_is_refused_not_set_aside(self, monkeypatch):
        # Security review (048, Medium): no key → the vault reads as {} and looked empty.
        monkeypatch.setattr(config, "_vault_unreadable", True)
        assert "locked credential vault" in relay_server.refusal()

    def test_main_exits_1_on_a_refusal(self, monkeypatch):
        monkeypatch.setenv("IG_PUBLIC_BASE_URL", "")
        assert relay_server.main([]) == 1


def test_the_relay_loads_none_of_the_app(tmp_path):
    # A fresh interpreter: this test process has imported the dashboard elsewhere.
    code = """
import io, os, config, relay_server
from fastapi.testclient import TestClient
from PIL import Image
assert relay_server.refusal() is None, relay_server.refusal()
config.save_settings({"ig_relay_open": True, "ig_public_base_url": os.environ["IG_PUBLIC_BASE_URL"]})
b = io.BytesIO(); Image.new("RGB", (8, 8)).save(b, "PNG")
c = TestClient(relay_server.build_app())
u = c.post("/api/ig/relay", files={"file": ("p.png", b.getvalue())}).json()["url"]
assert c.get(u.split("example.test")[1]).status_code == 200
print(config.DB_PATH.exists(), relay_server.refusal())
"""
    env = {**os.environ, "PAWPOLLER_APPDATA_DIR": str(tmp_path), "IG_PUBLIC_BASE_URL": "https://example.test"}
    env.pop("PAWPOLLER_HOSTED", None)
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip().splitlines()[-1] == "False None"
