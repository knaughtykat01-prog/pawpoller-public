"""Opt-in usage check-in (4.42.0, spec 009) — "count this copy".

The payload is a closed list of anonymous facts; nothing is sent unless the
separate ``tech_usage`` switch is on. httpx is replaced with a recorder, so no
test touches the network.
"""
from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import techcentre as tc
from routes.tech_api import tech_router

_REAL_PLATFORMS, _REAL_LIBRARY = tc._platforms, tc._library

FR002 = {"install_id", "version", "runtime", "mode", "os", "arch", "packaging", "open", "uptime_s", "rtt_ms",
         "platforms", "library"}


class Recorder:
    def __init__(self):
        self.calls: list[dict] = []
        self.status = 200

    def __call__(self, url, json=None, timeout=None, headers=None):
        self.calls.append({"url": url, "json": json, "headers": headers})
        import httpx
        return httpx.Response(self.status, json={"ok": True, "next_in": 300})


@pytest.fixture(autouse=True)
def rec(tmp_path, monkeypatch):
    settings: dict = {"setup_mode": "standalone"}
    monkeypatch.setattr(tc.config, "get_settings", lambda: dict(settings))
    monkeypatch.setattr(tc.config, "save_settings", lambda d: settings.update(d))
    monkeypatch.setattr(tc, "PENDING_PATH", tmp_path / "tech_pending.json")
    monkeypatch.setattr(tc, "STATE_PATH", tmp_path / "tech_state.json")
    monkeypatch.setattr(tc, "TECH_CENTRE_URL", "https://tech.example")
    monkeypatch.setattr(tc, "runtime", lambda: "desktop")
    monkeypatch.setattr(tc, "_platforms", lambda: ["bsky", "fa"])
    monkeypatch.setattr(tc, "_library", lambda: {"artwork": "11-100", "stories": "1-10"})
    monkeypatch.setenv("PAWPOLLER_TECH_CENTRE_THREAD", "0")
    r = Recorder()
    import httpx
    monkeypatch.setattr(httpx, "post", r)
    return r


def test_nothing_is_sent_until_the_switch_is_on(rec):
    assert tc.usage_consent() is None
    assert tc.checkin(force=True)["skipped"] == "off"
    tc.set_usage_consent(False)
    assert tc.checkin(force=True)["skipped"] == "off"
    assert rec.calls == []


def test_error_reports_yes_is_not_a_usage_yes(rec):
    tc.set_consent(True)
    tc.checkin(force=True)
    assert rec.calls == []


def test_payload_is_exactly_the_listed_facts(rec):
    tc.set_usage_consent(True)
    assert tc.checkin()["ok"]
    body = rec.calls[0]["json"]
    assert set(body) == FR002
    assert rec.calls[0]["url"].endswith("/api/v1/checkin")
    assert rec.calls[0]["headers"]["X-Syncopates-App"] == "pawpoller"
    assert body["platforms"] == ["bsky", "fa"] and body["library"] == {"artwork": "11-100", "stories": "1-10"}
    assert body["mode"] == "standalone" and body["runtime"] == "desktop" and body["version"] == tc.config.APP_VERSION
    assert len(json.dumps(body)) < 1024                       # SC-005


def test_preview_is_the_payload(rec):
    tc.set_usage_consent(True)
    preview = tc.checkin_payload()
    tc.checkin()
    sent = rec.calls[0]["json"]
    assert set(preview) == set(sent) and preview["install_id"] == sent["install_id"]


def test_cadence_and_rtt_carried_forward(rec):
    tc.set_usage_consent(True)
    tc.checkin()
    assert rec.calls[0]["json"]["rtt_ms"] is None             # nothing measured yet
    assert tc.checkin()["skipped"] == "not due"               # < 5 minutes later
    tc.checkin(force=True)
    assert isinstance(rec.calls[1]["json"]["rtt_ms"], int)    # the previous round trip rides along


def test_failure_backs_off(rec):
    tc.set_usage_consent(True)
    rec.status = 503
    assert not tc.checkin()["ok"]
    with tc._lock:
        st = tc._state()
    assert st["checkin_failures"] == 1 and st["next_checkin"] > 0
    assert tc.checkin()["skipped"] == "not due"


def test_open_reflects_recent_dashboard_use(rec, monkeypatch):
    monkeypatch.setattr(tc, "_ui_last", 0.0)
    assert tc.checkin_payload()["open"] is False
    tc.note_ui()
    assert tc.checkin_payload()["open"] is True


@pytest.mark.parametrize("n,band", [(0, "0"), (1, "1-10"), (10, "1-10"), (11, "11-100"), (100, "11-100"),
                                    (101, "101-1000"), (5000, "1000+")])
def test_bands(n, band):
    assert tc.band(n) == band


def test_packaging_source_when_not_frozen(monkeypatch):
    monkeypatch.delenv("APPIMAGE", raising=False)
    monkeypatch.setattr(tc.Path, "exists", lambda self: False)
    assert tc.packaging() == "source"
    monkeypatch.setenv("APPIMAGE", "/x/PawPoller.AppImage")
    assert tc.packaging() == "appimage"


def test_real_readers_never_raise():
    plats = _REAL_PLATFORMS()
    lib = _REAL_LIBRARY()
    assert isinstance(plats, list) and all(isinstance(p, str) for p in plats)
    assert set(lib) <= {"artwork", "stories"} and all(v in ("0", "1-10", "11-100", "101-1000", "1000+") for v in lib.values())


def test_routes(rec):
    app = FastAPI()
    app.include_router(tech_router)
    c = TestClient(app)
    st = c.post("/api/tech/usage", json={"value": True}).json()
    assert st["usage"] is True and st["usage_asked"] is True
    assert set(c.get("/api/tech/checkin-preview").json()) == FR002
    assert c.post("/api/tech/usage", json={}).status_code == 400


# ── the three places that ask ────────────────────────────────────────────────

def _js(name):
    from pathlib import Path
    return (Path(__file__).resolve().parent.parent / "frontend" / "js" / name).read_text(encoding="utf-8")


def test_wizard_asks_with_an_unticked_box():
    app = _js("app.js")
    box = app.split('id="setup-usage"', 1)[0].rsplit("<input", 1)[1] + 'id="setup-usage"' + app.split('id="setup-usage"', 1)[1].split(">", 1)[0]
    assert "checked" not in box
    assert "API.setTechUsage(usage)" in app


def test_existing_installs_are_asked_once_not_over_whats_new():
    app = _js("app.js")
    ask = app.split("async _maybeShowTechPrompt() {", 1)[1].split("\n    },", 1)[0]
    assert "!st.usage_asked" in ask and "_usageSnoozed" in ask and "whatsnew-ov" in ask
    assert "API.getCheckinPreview()" in app.split("async _showUsagePromptModal() {", 1)[1].split("\n    },", 1)[0]


def test_diagnostics_has_the_switch_and_preview():
    d = _js("diagnostics.js")
    assert 'id="tech-usage"' in d and "/api/tech/usage" in d and "/api/tech/checkin-preview" in d


def test_every_consent_text_names_the_id_and_country():
    """The Tech Centre stores a hashed random id and a two-letter country; saying
    "anonymous" without them would not be the whole truth (4.42.0 security review)."""
    app, diag = _js("app.js"), _js("diagnostics.js")
    wizard = app.split('id="setup-usage"', 1)[1].split("</label>", 1)[0]
    prompt = app.split("async _showUsagePromptModal() {", 1)[1].split("\n    },", 1)[0]
    panel = diag.split('id="tech-usage"', 1)[1].split("tech-usage-preview", 1)[0]
    for text in (wizard, prompt, panel):
        assert "random id" in text and "country" in text
