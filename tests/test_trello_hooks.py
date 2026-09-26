"""Trello's webhook callback — PawPoller's one unauthenticated route that takes a
body from the internet (spec 006, research R3). A delivery must be signed with the
app Secret, capped in size, rate-limited, and can only mark a board for re-reading.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

import config
from trello import mapping, runtime

SECRET = "s" * 32
CALLBACK = "https://pp.example.com/hooks/trello"


def _sign(body: bytes) -> str:
    return base64.b64encode(hmac.new(SECRET.encode(), body + CALLBACK.encode(),
                                     hashlib.sha1).digest()).decode()


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(config, "is_dashboard_auth_required", lambda: True)
    monkeypatch.setattr(config, "validate_api_key", lambda token: False)
    config.save_settings({"trello_secret": SECRET})
    mapping.save_config(webhooks="auto", webhook_callback=CALLBACK)
    runtime.take_dirty()
    from routes import trello_hooks
    trello_hooks._hits.clear()
    import dashboard
    return TestClient(dashboard.app, raise_server_exceptions=False)


def _payload(board="B1", kind="updateCard", card="C1"):
    return json.dumps({"model": {"id": board}, "action": {
        "type": kind, "data": {"board": {"id": board}, "card": {"id": card}}}}).encode()


def test_head_answers_200_without_a_login(client):
    """Trello will not create the webhook without it."""
    assert client.head("/hooks/trello").status_code == 200


def test_a_signed_delivery_marks_the_board_without_a_login(client):
    body = _payload()
    r = client.post("/hooks/trello", content=body, headers={"X-Trello-Webhook": _sign(body)})
    assert r.status_code == 200
    boards, cards = runtime.take_dirty()
    assert boards == {"B1"} and cards == {}


def test_a_comment_edit_marks_the_card_for_a_comment_re_read(client):
    body = _payload(kind="updateComment")
    client.post("/hooks/trello", content=body, headers={"X-Trello-Webhook": _sign(body)})
    assert runtime.take_dirty()[1] == {"C1": "B1"}


@pytest.mark.parametrize("sig", ["", "bm9wZQ=="])
def test_an_unsigned_or_wrongly_signed_delivery_is_refused(client, sig):
    r = client.post("/hooks/trello", content=_payload(), headers={"X-Trello-Webhook": sig})
    assert r.status_code == 401
    assert runtime.take_dirty() == (set(), {})


def test_without_a_secret_nothing_is_accepted(client):
    config.save_settings({"trello_secret": ""})
    body = _payload()
    r = client.post("/hooks/trello", content=body, headers={"X-Trello-Webhook": _sign(body)})
    assert r.status_code == 401


def test_an_oversized_body_is_refused(client):
    assert client.post("/hooks/trello", content=b"x" * (300 * 1024)).status_code == 413


def test_it_is_outside_the_loopback_only_prefix(client):
    """/api/trello is sensitive-when-open; the callback must not live under it."""
    import dashboard
    assert "/hooks/trello" in dashboard._AUTH_EXEMPT_PATHS
    assert not "/hooks/trello".startswith("/api/trello")


def test_the_route_never_logs_the_body():
    src = open("routes/trello_hooks.py", encoding="utf-8").read()
    for line in src.splitlines():
        if "logger." in line:
            assert "body" not in line and "payload" not in line, line
