"""4.46.1: the 4.46.0 release review's High — the artwork push and the mirror target sent the stored
server key wherever the request said, and the artwork push packed any folder it was named."""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import config


@pytest.fixture
def local(monkeypatch):
    import dashboard
    monkeypatch.setattr(config, "is_dashboard_auth_required", lambda: False)
    return TestClient(dashboard.app, raise_server_exceptions=False, client=("127.0.0.1", 50000))


def test_artwork_push_ignores_a_server_named_in_the_request(local, tmp_path, monkeypatch):
    archive = tmp_path / "art"
    (archive / "Sample_Piece").mkdir(parents=True)
    monkeypatch.setattr("posting.artwork_reader.get_artwork_archive_path", lambda: archive)
    r = local.post("/api/artwork/sync/push", json={"server_url": "https://elsewhere.example", "api_key": "pp_x"})
    assert r.status_code == 400 and "No server URL configured" in r.text


@pytest.mark.parametrize("name", ["../outside", "..", "Sample_Piece/../../outside", "/etc", "a/b"])
def test_artwork_push_keeps_the_name_inside_the_archive(local, tmp_path, monkeypatch, name):
    archive = tmp_path / "art"
    (archive / "Sample_Piece").mkdir(parents=True)
    (tmp_path / "outside").mkdir()
    monkeypatch.setattr("posting.artwork_reader.get_artwork_archive_path", lambda: archive)
    config.save_settings({"posting_server_url": "http://127.0.0.1:9"})
    r = local.post("/api/artwork/sync/push", json={"artwork_name": name})
    assert r.status_code == 400 and "Invalid artwork name" in r.text


def test_the_stored_key_only_goes_to_the_saved_server():
    from routes.mirror_api import _mirror_target
    config.save_settings({"posting_server_url": "https://saved.example", "posting_server_api_key": "pp_stored"})
    assert _mirror_target({}) == ("https://saved.example", "pp_stored")
    assert _mirror_target({"server_url": "https://saved.example/"}) == ("https://saved.example", "pp_stored")
    # A typed server: seeding still works with the key typed beside it, never the stored one.
    assert _mirror_target({"server_url": "https://other.example", "api_key": "pp_typed"}) == \
        ("https://other.example", "pp_typed")
    assert _mirror_target({"server_url": "https://other.example"}) == ("https://other.example", "")


def test_push_routes_are_locked_on_an_open_instance(monkeypatch):
    import dashboard
    monkeypatch.setattr(config, "is_dashboard_auth_required", lambda: False)
    remote = TestClient(dashboard.app, raise_server_exceptions=False)
    for path in ("/api/artwork/sync/push", "/api/posting/sync/push"):
        r = remote.post(path, json={})
        assert r.status_code == 403, (path, r.status_code)


def test_mirror_target_still_refuses_plain_http():
    from routes.mirror_api import _mirror_target
    with pytest.raises(HTTPException):
        _mirror_target({"server_url": "http://203.0.113.5:8420", "api_key": "pp_typed"})
