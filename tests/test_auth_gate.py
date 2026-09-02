"""Regression tests for the open-instance credential-endpoint gate (2.46.0).

Security fix: an unconfigured server (no dashboard password) used to serve
*every* endpoint with no auth — including POST /api/settings/sync, which
returns all stored platform credentials in cleartext. The middleware now
refuses the credential/backup/destructive endpoints for a remote (non-loopback)
caller when auth isn't configured, while leaving them reachable from loopback
(the desktop app / local operator) and leaving non-sensitive endpoints open.

FastAPI's TestClient presents a peer host of "testclient" (non-loopback), so it
exercises the remote-caller path. `is_dashboard_auth_required` is monkeypatched
so the tests don't depend on ambient settings.
"""
import config
from fastapi.testclient import TestClient


def _client() -> TestClient:
    import dashboard
    return TestClient(dashboard.app)


def test_open_instance_gates_credential_sync_from_remote(monkeypatch):
    # Open instance: no auth configured.
    monkeypatch.setattr(config, "is_dashboard_auth_required", lambda: False)
    r = _client().post("/api/settings/sync", json={"mode": "pull"})
    assert r.status_code == 403, f"expected 403, got {r.status_code}"


def test_open_instance_gates_backup_and_upload_from_remote(monkeypatch):
    monkeypatch.setattr(config, "is_dashboard_auth_required", lambda: False)
    c = _client()
    assert c.get("/api/backup/database").status_code == 403
    assert c.post("/api/posting/sync/upload").status_code == 403


def test_open_instance_leaves_nonsensitive_endpoints_open(monkeypatch):
    # A genuinely open (loopback-only) instance must still work locally.
    monkeypatch.setattr(config, "is_dashboard_auth_required", lambda: False)
    assert _client().get("/api/health").status_code == 200


def test_configured_instance_requires_auth_on_sync(monkeypatch):
    # When a password IS set, the sensitive endpoint falls through to the
    # normal auth check and rejects an unauthenticated caller (401, not 403).
    monkeypatch.setattr(config, "is_dashboard_auth_required", lambda: True)
    r = _client().post("/api/settings/sync", json={"mode": "pull"})
    assert r.status_code == 401, f"expected 401, got {r.status_code}"
