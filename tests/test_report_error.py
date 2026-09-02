"""Error-report endpoint (2.159.0): POST /api/report-error → Telegram.

The "Send to dev" button on the error popup. The report must always land in the
server log, forward via the instance's Telegram when configured (sent: true),
HTML-escape everything user-supplied (parse_mode=HTML), and clip hostile-length
fields so a report can't blow Telegram's 4096-char message limit.
"""
import pytest
from fastapi.testclient import TestClient

import config


def _client():
    from dashboard import app
    return TestClient(app)


@pytest.fixture
def captured(monkeypatch):
    """Stub the Telegram send; captures the formatted message text."""
    box = {"text": None, "ok": True}

    async def fake_send(text):
        box["text"] = text
        return box["ok"]

    import routes.report_api as mod
    monkeypatch.setattr(mod, "send_telegram", fake_send)
    return box


def test_report_forwards_to_telegram(captured):
    r = _client().post("/api/report-error", json={
        "context": "POST /api/artwork/import → 400",
        "message": "Stored URL did not return an image",
        "detail": '{"detail": "Stored URL did not return an image"}',
        "url": "#/library",
        "version": "2.159.0",
        "ua": "TestBrowser/1.0",
    })
    assert r.status_code == 200
    assert r.json() == {"sent": True}
    text = captured["text"]
    assert "PawPoller error report" in text
    assert "POST /api/artwork/import" in text
    assert "#/library" in text
    assert "2.159.0" in text
    assert "Stored URL did not return an image" in text


def test_sent_false_when_telegram_unconfigured(captured):
    captured["ok"] = False
    r = _client().post("/api/report-error", json={"message": "boom"})
    assert r.status_code == 200
    assert r.json() == {"sent": False}


def test_user_content_is_html_escaped(captured):
    _client().post("/api/report-error", json={
        "context": "<script>alert(1)</script>",
        "detail": "a <b>bold</b> claim",
    })
    text = captured["text"]
    assert "<script>" not in text
    assert "&lt;script&gt;" in text
    assert "&lt;b&gt;bold&lt;/b&gt;" in text


def test_oversized_fields_are_clipped(captured):
    _client().post("/api/report-error", json={
        "context": "x" * 5000,
        "message": "y" * 5000,
        "detail": "z" * 50000,
        "url": "u" * 5000,
        "ua": "w" * 5000,
    })
    # Telegram's hard cap is 4096 chars — the clipped report must fit.
    assert len(captured["text"]) < 4096


def test_empty_report_still_ok_and_stamps_server_version(captured):
    r = _client().post("/api/report-error", json={})
    assert r.status_code == 200
    assert config.APP_VERSION in captured["text"]
