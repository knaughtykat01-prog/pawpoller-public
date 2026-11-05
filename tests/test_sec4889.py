"""SEC4889 (4.88.9 release review, Medium): the server key goes only over https, loopback or Tailscale.

Pairing through setup mode saved a plain-http address unchecked, and the story push, the artwork push
and the Instagram image relay sent the stored key to whatever address was saved. Each now checks where
the key is sent, so an address saved before this (or by any other route) can't carry it either.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

import config

LAN = "http://192.168.1.20:8420"


@pytest.fixture
def local(monkeypatch):
    import dashboard
    monkeypatch.setattr(config, "is_dashboard_auth_required", lambda: False)
    return TestClient(dashboard.app, raise_server_exceptions=False, client=("127.0.0.1", 50000))


def _no_network(monkeypatch):
    import httpx

    class Boom:
        def __init__(self, *a, **k):
            raise AssertionError("the key must not be sent")

    monkeypatch.setattr(httpx, "AsyncClient", Boom)


def test_pairing_refuses_a_plain_http_server(local):
    r = local.post("/api/settings/setup-mode", json={"mode": "paired_desktop", "posting_server_url": LAN,
                                                      "posting_server_api_key": "pp_x"})
    assert r.status_code == 400 and "Tailscale" in r.json()["detail"]
    assert config.get_settings().get("posting_server_url", "") != LAN


def test_pairing_over_tailscale_still_works(local):
    r = local.post("/api/settings/setup-mode", json={"mode": "paired_desktop",
                                                      "posting_server_url": "http://box.tail1.ts.net:8420/",
                                                      "posting_server_api_key": "pp_x"})
    assert r.status_code == 200, r.text
    assert config.get_settings()["posting_server_url"] == "http://box.tail1.ts.net:8420"


def test_story_push_refuses_a_stored_plain_http_server(local, story_archive, monkeypatch):
    config.save_settings({"posting_server_url": LAN, "posting_server_api_key": "pp_x",
                          "posting_story_archive_path": str(story_archive)})
    _no_network(monkeypatch)
    r = local.post("/api/posting/sync/push", json={})
    assert r.status_code == 400 and "Server URL" in r.text


def test_artwork_push_refuses_a_stored_plain_http_server(local, tmp_path, monkeypatch):
    archive = tmp_path / "art"
    (archive / "Sample_Piece").mkdir(parents=True)
    monkeypatch.setattr("posting.artwork_reader.get_artwork_archive_path", lambda: archive)
    config.save_settings({"posting_server_url": LAN, "posting_server_api_key": "pp_x"})
    _no_network(monkeypatch)
    r = local.post("/api/artwork/sync/push", json={})
    assert r.status_code == 400 and "Server URL" in r.text


def test_image_relay_refuses_plain_http(tmp_path, monkeypatch):
    from posting import post_publisher
    p = tmp_path / "a.png"
    p.write_bytes(b"x")
    _no_network(monkeypatch)
    with pytest.raises(RuntimeError, match="refused"):
        asyncio.run(post_publisher._relay_stash_image(LAN, "pp_x", str(p)))


# Release review (4.88.9, Medium): the four other places that send the stored key check the address too.

def test_drift_check_refuses_a_stored_plain_http_server(monkeypatch):
    from fastapi import HTTPException

    from routes import mirror_api
    config.save_settings({"posting_server_url": LAN, "posting_server_api_key": "pp_x"})
    _no_network(monkeypatch)
    with pytest.raises(HTTPException) as e:
        asyncio.run(mirror_api.compute_drift())
    assert e.value.status_code == 400 and "Server URL" in e.value.detail


def test_startup_settings_pull_skips_a_plain_http_server(monkeypatch):
    import inspect

    import main
    src = inspect.getsource(main)
    i = src.index('f"{server_url}/api/settings/sync"')
    assert "is_trusted_transport(server_url)" in src[max(0, src.rfind("\ndef ", 0, i)):i]


def test_instagram_upload_with_a_key_refuses_plain_http(tmp_path, monkeypatch):
    from posting import ig_host
    p = tmp_path / "a.png"
    p.write_bytes(b"x")
    _no_network(monkeypatch)
    with pytest.raises(RuntimeError, match="refused"):
        asyncio.run(ig_host.upload_to_host(LAN + "/api/ig/pubmedia", str(p), "pp_x"))


def test_telegram_sync_checks_the_address():
    import inspect

    from polling import telegram_bot
    src = inspect.getsource(telegram_bot)
    i = src.index('/api/posting/sync/upload')
    assert "is_trusted_transport(server_url)" in src[src.rfind("posting_server_api_key", 0, i):i]
