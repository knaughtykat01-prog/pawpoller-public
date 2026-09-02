"""Itaku auth token can be set/cleared for posting (2.191.0).

The Itaku settings panel only had a username field ("No auth required"), but
posting to Itaku needs an auth token — with nowhere to enter it. This adds the
token to the connect payload + a standalone set/clear endpoint, surfaced by the
auth-status `has_auth_token` flag.
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import config
from clients.ik.client import IKClient
from routes.ik_api import ik_router


def _client():
    app = FastAPI()
    app.include_router(ik_router)
    return TestClient(app)


@pytest.fixture(autouse=True)
def _accept_any_token(monkeypatch):
    """Since 3.9.2 the route checks the token with Itaku before saving it.

    These tests are about vault routing and clearing, not validation, and
    without this they make a real request to itaku.ee — which is both a live
    dependency in a unit test and a guaranteed 401, since "secret-abc" is not a
    real token. Validation has its own tests in test_itaku_token_validation.py.
    """
    async def _ok(self, token):
        return {"status": "ok", "username": "secondfur"}
    monkeypatch.setattr(IKClient, "validate_token", _ok)


def test_status_reports_has_auth_token():
    c = _client()
    assert c.get("/api/ik/auth/status").json()["has_auth_token"] is False
    config.save_settings({"ik_auth_token": "tok-123"})
    assert c.get("/api/ik/auth/status").json()["has_auth_token"] is True


def test_set_token_saves_to_vault_and_clear_removes_it():
    c = _client()
    r = c.post("/api/ik/auth/token", json={"auth_token": "secret-abc"})
    assert r.status_code == 200 and r.json()["status"] == "success"
    # It's a credential field → routed to the vault, not plaintext settings.
    assert config.get_settings().get("ik_auth_token") == "secret-abc"
    assert config.is_credential_key("ik_auth_token")

    # Empty token clears it.
    r2 = c.post("/api/ik/auth/token", json={"auth_token": "  "})
    assert r2.json()["status"] == "cleared"
    assert not config.get_settings().get("ik_auth_token")


def test_disconnect_clears_token_too():
    c = _client()
    config.save_settings({"ik_target_user": "knaughtykat", "ik_auth_token": "t"})
    c.post("/api/ik/auth/disconnect")
    s = config.get_settings()
    assert not s.get("ik_target_user")
    assert not s.get("ik_auth_token")
