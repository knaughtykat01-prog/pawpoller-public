"""Itaku auth-token validation at save time (3.9.2).

The token is not needed for tracking, so a wrong one used to sit in settings
looking connected until the next upload failed. That is how the 2026-08-19
`Showing_Off` post died with a 401 nobody had been warned about.
"""
from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import config
from clients.ik.client import IKClient
from routes import ik_api


class _FakeResponse:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def _client_returning(monkeypatch, response):
    ik = IKClient("secondfur")

    class _FakeHttp:
        async def get(self, *a, **kw):
            if isinstance(response, Exception):
                raise response
            return response

        async def aclose(self):
            pass

    monkeypatch.setattr(ik, "_http", _FakeHttp())
    return ik


@pytest.mark.asyncio
async def test_a_working_token_reports_who_it_is(monkeypatch):
    ik = _client_returning(monkeypatch, _FakeResponse(200, {"username": "secondfur"}))
    result = await ik.validate_token("abc123")
    assert result == {"status": "ok", "username": "secondfur"}


@pytest.mark.asyncio
async def test_a_wrong_token_is_invalid_not_malformed(monkeypatch):
    ik = _client_returning(monkeypatch, _FakeResponse(401, {"detail": "Invalid token."}))
    result = await ik.validate_token("abc123")
    assert result["status"] == "invalid"


@pytest.mark.asyncio
async def test_a_bad_header_is_reported_as_malformed(monkeypatch):
    """Itaku's own words for this blame the token, but the fault is the header
    shape — telling them apart is the point of having two statuses."""
    ik = _client_returning(monkeypatch, _FakeResponse(
        401, {"detail": "Invalid token header. Token string should not contain spaces."}))
    result = await ik.validate_token("Token abc 123")
    assert result["status"] == "malformed"


@pytest.mark.asyncio
async def test_a_network_failure_is_not_called_a_bad_token(monkeypatch):
    """Saying "bad token" here would send the user hunting for a new one."""
    ik = _client_returning(monkeypatch, httpx.ConnectError("boom"))
    result = await ik.validate_token("abc123")
    assert result["status"] == "error"


@pytest.mark.asyncio
async def test_an_empty_token_is_rejected_without_a_request(monkeypatch):
    ik = IKClient("secondfur")
    result = await ik.validate_token("   ")
    assert result["status"] == "invalid"


# ── The route ─────────────────────────────────────────────────

@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(config, "VAULT_PATH", tmp_path / "settings.vault.json")
    config.SETTINGS_PATH.write_text("{}", encoding="utf-8")
    app = FastAPI()
    app.include_router(ik_api.ik_router)
    return TestClient(app)


def _stub_validate(monkeypatch, result):
    async def fake(self, token):
        return result
    monkeypatch.setattr(IKClient, "validate_token", fake)


def test_a_rejected_token_is_not_saved(monkeypatch, client):
    _stub_validate(monkeypatch, {"status": "invalid", "detail": "Invalid token."})
    r = client.post("/api/ik/auth/token", json={"auth_token": "sessionid-not-a-token"})
    assert r.status_code == 401
    assert not config.get_settings().get("ik_auth_token")


def test_the_rejection_says_it_is_not_the_session_cookie(monkeypatch, client):
    """The mistake worth naming: the Cookie header's `sessionid` looks like a
    credential and is the wrong one."""
    _stub_validate(monkeypatch, {"status": "invalid", "detail": "Invalid token."})
    r = client.post("/api/ik/auth/token", json={"auth_token": "xtwtkmk8tbebjd"})
    assert "sessionid" in r.json()["detail"]
    assert "Authorization" in r.json()["detail"]


def test_a_malformed_header_gets_its_own_message(monkeypatch, client):
    _stub_validate(monkeypatch, {"status": "malformed", "detail": "..."})
    r = client.post("/api/ik/auth/token", json={"auth_token": "Token abc 123"})
    assert r.status_code == 400
    assert "space" in r.json()["detail"]


def test_a_working_token_is_saved(monkeypatch, client):
    _stub_validate(monkeypatch, {"status": "ok", "username": "secondfur"})
    r = client.post("/api/ik/auth/token", json={"auth_token": "abc123"})
    assert r.status_code == 200
    assert "secondfur" in r.json()["message"]
    assert config.get_settings().get("ik_auth_token") == "abc123"


def test_an_empty_token_still_clears_without_calling_itaku(client):
    config.save_settings({"ik_auth_token": "abc123"})
    r = client.post("/api/ik/auth/token", json={"auth_token": ""})
    assert r.status_code == 200
    assert r.json()["status"] == "cleared"
    assert not config.get_settings().get("ik_auth_token")


def test_a_network_failure_does_not_discard_a_possibly_good_token(monkeypatch, client):
    _stub_validate(monkeypatch, {"status": "error", "detail": "Could not reach Itaku"})
    r = client.post("/api/ik/auth/token", json={"auth_token": "abc123"})
    assert r.status_code == 502
    assert not config.get_settings().get("ik_auth_token")
