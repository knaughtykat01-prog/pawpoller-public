"""4.73.0 (spec 038): /api/health/details — private, counts and states only."""
from __future__ import annotations

from fastapi.testclient import TestClient

import config

TOKEN = "t" * 43
AUTH = {"Authorization": f"Bearer {TOKEN}"}
KEYS = {"version", "commit", "health", "problems", "polling", "sites", "queue", "last_check_at", "tech_id"}


def _client():
    import dashboard
    dashboard._auth_failures.clear()
    return TestClient(dashboard.app, raise_server_exceptions=False)


def _log(table, status, error=None, started="2026-10-14 03:00:00"):
    from database.db import get_connection
    conn = get_connection()
    conn.execute(f"INSERT INTO {table}(status, error_message, started_at) VALUES(?,?,?)", (status, error, started))
    conn.commit()
    conn.close()


def test_absent_without_the_token_env(monkeypatch):
    monkeypatch.delenv("PAWPOLLER_DETAILS_TOKEN", raising=False)
    assert _client().get("/api/health/details", headers=AUTH).status_code == 404


def test_wrong_or_missing_token_is_401(monkeypatch):
    monkeypatch.setenv("PAWPOLLER_DETAILS_TOKEN", TOKEN)
    monkeypatch.setattr(config, "is_dashboard_auth_required", lambda: True)
    c = _client()
    assert c.get("/api/health/details").status_code == 401
    assert c.get("/api/health/details", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_sites_queue_and_no_handle_leaks(monkeypatch):
    monkeypatch.setenv("PAWPOLLER_DETAILS_TOKEN", TOKEN)
    monkeypatch.setattr(config, "is_dashboard_auth_required", lambda: True)
    real = config.get_settings
    monkeypatch.setattr(config, "get_settings", lambda: {**real(), "fa_username": "Inkwolf"})
    _log("fa_poll_log", "success")
    _log("ao3_poll_log", "error", "Login failed for Inkwolf at https://example.com/users/Inkwolf")
    _log("e621_poll_log", "running")
    r = _client().get("/api/health/details", headers=AUTH)
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) <= KEYS
    sites = {s["site"]: s for s in body["sites"]}
    assert sites["fa"]["result"] == "ok" and sites["fa"]["at"] == "2026-10-14T03:00:00Z"
    assert sites["ao3"]["result"] == "failed" and sites["e621"]["result"] == "running"
    assert "Inkwolf" not in r.text and "example.com/users" not in r.text
    assert "ws" not in sites                      # never polled → not listed
    assert body["queue"] == {"waiting": 0, "failed": 0} and body["polling"] in ("on", "paused")
    assert body["last_check_at"] == "2026-10-14T03:00:00Z"


def test_a_newer_skip_wins_an_older_one_loses(monkeypatch):
    from polling import last_check
    monkeypatch.setenv("PAWPOLLER_DETAILS_TOKEN", TOKEN)
    monkeypatch.setattr(last_check, "_skips", {"fa": ("2026-10-14T04:00:00Z", "waited 10 minutes for its turn"),
                                               "bsky": ("2026-10-14T01:00:00Z", "waited")})
    _log("fa_poll_log", "success")
    _log("bsky_poll_log", "success")
    sites = {s["site"]: s for s in _client().get("/api/health/details", headers=AUTH).json()["sites"]}
    assert sites["fa"]["result"] == "skipped" and sites["fa"]["reason"].startswith("waited")
    assert sites["bsky"]["result"] == "ok"


def test_a_turn_timeout_records_the_skip():
    from polling import last_check
    from polling.site_turns import TurnTimeout
    last_check._skips.clear()
    TurnTimeout("fa")
    assert last_check.skips()["fa"][1] == "waited 10 minutes for its turn"
