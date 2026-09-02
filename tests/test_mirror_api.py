"""Route-level tests for the mirror API.

The unit tests in ``test_mirror.py`` prove the primitives. These prove the
*wiring*, which is where the damage would be: a pull that runs on the server
overwrites the authoritative data with the subordinate copy, and a name
parameter that is not re-checked walks out of the archive root. Neither shows
up in a test of ``mirror.core``.
"""
from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

import config
from routes import mirror_api


@pytest.fixture
def client(monkeypatch, tmp_path):
    # The mirror API is in _SENSITIVE_WHEN_OPEN_PREFIXES, so an unconfigured
    # instance refuses it outright to a non-loopback caller — and TestClient is
    # not loopback. Authenticate properly rather than weakening the middleware,
    # which is also how the endpoint is reached in real use.
    monkeypatch.setattr(config, "is_dashboard_auth_required", lambda: True)
    monkeypatch.setattr(config, "validate_api_key", lambda token: token == "pp_test")
    artwork = tmp_path / "artwork"
    (artwork / "A_Piece").mkdir(parents=True)
    (artwork / "A_Piece" / "image.png").write_bytes(b"pixels")
    (artwork / "A_Piece" / "masterpiece.json").write_text("{}")
    media = tmp_path / "posts_media"
    media.mkdir()
    (media / "7_0.png").write_bytes(b"media")

    monkeypatch.setattr(mirror_api, "_artwork_root", lambda: artwork)
    monkeypatch.setattr(mirror_api, "_posts_media_root", lambda: media)

    import dashboard
    return TestClient(dashboard.app, raise_server_exceptions=False,
                      headers={"Authorization": "Bearer pp_test"})


# ── Serving half ──────────────────────────────────────────────

def test_manifest_lists_stores(client):
    body = client.get("/api/mirror/manifest").json()
    assert body["artwork"]["count"] == 1
    assert body["artwork"]["folders"][0]["name"] == "A_Piece"
    assert body["posts_media"]["count"] == 1
    assert "session_cache" in body["database"]["excluded_tables"]


def test_manifest_detail_is_opt_in(client):
    compact = client.get("/api/mirror/manifest").json()
    assert "files" not in compact["artwork"]["folders"][0]
    detailed = client.get("/api/mirror/manifest?detail=true").json()
    assert len(detailed["artwork"]["folders"][0]["files"]) == 2


def test_folder_download_returns_a_tarball(client):
    r = client.get("/api/mirror/artwork/A_Piece")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/gzip"
    assert len(r.content) > 0


def test_unknown_folder_is_404(client):
    assert client.get("/api/mirror/artwork/Nope").status_code == 404


@pytest.mark.parametrize("name", [
    "..",
    "../../etc",
    "..%2F..%2Fetc",
])
def test_folder_name_cannot_escape_the_archive_root(client, name):
    """A proxy may hand us an already-decoded name, so the check has to be on
    the resolved path rather than on the raw string."""
    r = client.get(f"/api/mirror/artwork/{name}")
    assert r.status_code in (400, 404), f"{name!r} was not rejected"


def test_db_snapshot_is_served_and_valid(client, tmp_path, monkeypatch):
    db = tmp_path / "pawpoller.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE accounts (account_id INTEGER PRIMARY KEY)")
    conn.execute("INSERT INTO accounts VALUES (1)")
    conn.commit()
    conn.close()
    monkeypatch.setattr(config, "DB_PATH", db)

    r = client.get("/api/mirror/db-snapshot")
    assert r.status_code == 200
    out = tmp_path / "received.db"
    out.write_bytes(r.content)
    got = sqlite3.connect(str(out))
    assert got.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 1
    got.close()


def test_snapshot_does_not_leave_temp_files_behind(client, tmp_path, monkeypatch):
    db = tmp_path / "pawpoller.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE accounts (account_id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()
    monkeypatch.setattr(config, "DB_PATH", db)

    client.get("/api/mirror/db-snapshot")
    assert not list(tmp_path.glob("*.snapshot.*")), "snapshot temp file was not cleaned up"


# ── Driving half ──────────────────────────────────────────────

def test_pull_is_refused_on_the_server(client, monkeypatch):
    """The most destructive call in this module: the server is the source of
    truth, so pulling *into* it overwrites real data with the copy."""
    monkeypatch.setattr("posting.scheduler.detect_runtime_mode", lambda: "server")
    r = client.post("/api/mirror/pull", json={"server_url": "https://example.invalid"})
    assert r.status_code == 409
    assert "mirror source" in r.json()["detail"]


def test_pull_refuses_plain_http_to_a_remote_host(client, monkeypatch):
    """The payload carries a bearer token and every row in the database."""
    monkeypatch.setattr("posting.scheduler.detect_runtime_mode", lambda: "desktop")
    r = client.post("/api/mirror/pull", json={"server_url": "http://203.0.113.10:8420"})
    assert r.status_code == 400
    assert "plain HTTP" in r.json()["detail"]


def test_pull_requires_a_server_url(client, monkeypatch):
    monkeypatch.setattr("posting.scheduler.detect_runtime_mode", lambda: "desktop")
    monkeypatch.setattr(config, "get_settings", lambda: {})
    r = client.post("/api/mirror/pull", json={})
    assert r.status_code == 400


def test_pull_status_is_readable_before_any_pull(client):
    body = client.get("/api/mirror/pull/status").json()
    assert body["running"] is False
    assert "phase" in body


# ── Work handoff (Stage 2) ────────────────────────────────────

def test_handoff_jobs_is_served(client):
    body = client.get("/api/mirror/handoff/jobs").json()
    assert "jobs" in body


def test_claim_requires_a_queue_id(client):
    assert client.post("/api/mirror/handoff/claim", json={}).status_code == 400


def test_claiming_an_unknown_job_is_409(client):
    """Not 404: 'no longer pending' is the same answer whether the row never
    existed or another worker took it, and the caller does the same thing."""
    r = client.post("/api/mirror/handoff/claim", json={"origin_queue_id": 999999})
    assert r.status_code == 409


def test_result_for_an_unknown_job_is_404(client):
    r = client.post("/api/mirror/handoff/result", json={
        "origin_queue_id": 999999, "platform": "fa",
        "story_name": "Nope", "success": True})
    assert r.status_code == 404


def test_result_with_missing_fields_is_400(client):
    r = client.post("/api/mirror/handoff/result", json={"origin_queue_id": 1})
    assert r.status_code == 400


@pytest.mark.parametrize("path", ["/api/mirror/handoff/pull", "/api/mirror/handoff/report"])
def test_the_driving_half_is_refused_on_the_server(client, monkeypatch, path):
    """The jobs exist *because* the server cannot execute them, so running the
    worker side there is always wrong."""
    monkeypatch.setattr("posting.scheduler.detect_runtime_mode", lambda: "server")
    r = client.post(path, json={"server_url": "https://example.invalid"})
    assert r.status_code == 409
    assert "worker" in r.json()["detail"]


@pytest.mark.parametrize("path", ["/api/mirror/handoff/pull", "/api/mirror/handoff/report"])
def test_the_driving_half_refuses_plain_http(client, monkeypatch, path):
    monkeypatch.setattr("posting.scheduler.detect_runtime_mode", lambda: "desktop")
    r = client.post(path, json={"server_url": "http://203.0.113.10:8420"})
    assert r.status_code == 400


# ── Auth posture ──────────────────────────────────────────────

def test_unconfigured_instance_refuses_a_remote_mirror_call(monkeypatch):
    """With no dashboard password set, /api/mirror must not answer a remote
    caller — db-snapshot would otherwise hand the entire database to anyone who
    found the port. Asserted through the middleware, not against the constant."""
    monkeypatch.setattr(config, "is_dashboard_auth_required", lambda: False)
    import dashboard
    c = TestClient(dashboard.app, raise_server_exceptions=False)
    for path in ("/api/mirror/manifest", "/api/mirror/db-snapshot"):
        assert c.get(path).status_code == 403, f"{path} answered an open instance"


def test_artwork_sync_upload_is_also_sensitive():
    """It extracts an attacker-supplied tar into the archive — a write
    primitive that was missing from this list until 3.6.0."""
    import dashboard
    assert any("/api/artwork/sync/upload".startswith(p)
               for p in dashboard._SENSITIVE_WHEN_OPEN_PREFIXES)


def test_mirror_routes_require_auth_when_configured(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "is_dashboard_auth_required", lambda: True)
    import dashboard
    c = TestClient(dashboard.app, raise_server_exceptions=False)
    for path in ("/api/mirror/manifest", "/api/mirror/db-snapshot",
                 "/api/mirror/artwork/Anything", "/api/mirror/pull/status"):
        assert c.get(path).status_code == 401, f"{path} answered without auth"
