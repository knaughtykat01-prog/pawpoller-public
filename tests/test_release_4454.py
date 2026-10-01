"""4.45.4: FRIENDGUARD, SYNCPUSH and the update notification. (PODLOCK and TESTKEYLEAK
live in test_open_instance_publish.py.)"""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

import config
from database import accounts as accounts_db
from database.db import get_connection


@pytest.fixture
def local(monkeypatch):
    import dashboard
    monkeypatch.setattr(config, "is_dashboard_auth_required", lambda: False)
    return TestClient(dashboard.app, raise_server_exceptions=False, client=("127.0.0.1", 50000))


def _account(platform: str, default: bool = False) -> int:
    conn = get_connection()
    try:
        return accounts_db.create_account(conn, platform, "Someone else's", is_default=default)
    finally:
        conn.close()


# ── FRIENDGUARD ──────────────────────────────────────────────────────────────

def test_a_never_post_account_is_refused_on_every_path():
    from posting import manager, post_publisher
    theirs, mine = _account("fa"), _account("fa")
    config.save_settings({"never_post_account_ids": [theirs]})
    with pytest.raises(ValueError, match="Never post"):
        manager._resolve_account_id("fa", theirs)
    with pytest.raises(ValueError, match="Never post"):
        manager._get_poster("fa", theirs)                  # edits and syncs build a poster here
    assert manager._get_poster("fa", mine) is not None     # the others are untouched
    bsky = _account("bsky")
    config.save_settings({"never_post_account_ids": [theirs, bsky]})
    r = asyncio.run(post_publisher._publish_one({"body": "hello"}, "bsky", bsky, None))
    assert not r["success"] and "Never post" in r["error"]


def test_the_default_account_is_checked_too():
    from posting import manager
    theirs = _account("ws", default=True)
    config.save_settings({"never_post_account_ids": [theirs]})
    with pytest.raises(ValueError, match="Never post"):
        manager._get_poster("ws")                          # None = the platform default


def test_never_post_is_set_per_account_from_settings(local):
    aid = _account("fa")
    assert local.patch(f"/api/accounts/{aid}", json={"never_post": True}).json()["account"]["never_post"]
    assert accounts_db.never_post_ids() == {aid}
    row = next(a for a in local.get("/api/accounts").json()["accounts"] if a["account_id"] == aid)
    assert row["never_post"] is True
    local.patch(f"/api/accounts/{aid}", json={"never_post": False})
    assert accounts_db.never_post_ids() == set()


def test_no_account_id_ships_in_the_code():
    import inspect
    assert "never_post_account_ids" in inspect.getsource(accounts_db.never_post_ids)
    config.save_settings({"never_post_account_ids": ["x", None]})     # junk is ignored, not fatal
    assert accounts_db.never_post_ids() == set()


# ── SYNCPUSH ─────────────────────────────────────────────────────────────────

def test_sync_push_ignores_a_server_named_in_the_request(local, tmp_path, monkeypatch):
    archive = tmp_path / "archive"
    (archive / "Sample_Story").mkdir(parents=True)
    monkeypatch.setattr("posting.story_reader.get_archive_path", lambda: archive)
    r = local.post("/api/posting/sync/push", json={"server_url": "https://elsewhere.example", "api_key": "pp_x"})
    assert r.status_code == 400 and "No server URL configured" in r.text


@pytest.mark.parametrize("name", ["../outside", "..", "Sample_Story/../../outside", "/etc", "a/b"])
def test_sync_push_keeps_the_story_inside_the_archive(local, tmp_path, monkeypatch, name):
    archive = tmp_path / "archive"
    (archive / "Sample_Story").mkdir(parents=True)
    (tmp_path / "outside").mkdir()
    monkeypatch.setattr("posting.story_reader.get_archive_path", lambda: archive)
    config.save_settings({"posting_server_url": "http://127.0.0.1:9"})
    r = local.post("/api/posting/sync/push", json={"story_name": name})
    assert r.status_code == 400 and "Invalid story name" in r.text


# ── Update notification ──────────────────────────────────────────────────────

def test_a_newer_release_shows_in_the_bell(local, monkeypatch):
    import updater
    monkeypatch.setattr(updater, "LATEST", {})
    updater._remember("99.0.0", True)
    items = local.get("/api/notifications").json()["items"]
    up = [i for i in items if i["kind"] == "update"]
    assert len(up) == 1 and "99.0.0" in up[0]["summary"] and up[0]["unread"]
    first_seen = updater.LATEST["seen_at"]
    updater._remember("99.0.0", True)
    assert updater.LATEST["seen_at"] == first_seen          # the same release isn't new twice
    config.save_settings({"update_skip_version": "99.0.0"})
    assert not [i for i in local.get("/api/notifications").json()["items"] if i["kind"] == "update"]
    updater._remember("99.0.0", False)                       # up to date → nothing to say
    assert updater.LATEST == {}
