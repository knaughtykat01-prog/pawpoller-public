"""Sign-up's once-only offers (4.69.0, spec 033 US5 + US4 screen + US6)."""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import config

ROOT = Path(__file__).resolve().parents[1]
_KEYS = ["announce_defaults_asked", "twofa_offered", "pb_import_asked"]
_LOCAL = ("127.0.0.1", 50000)


@pytest.fixture(autouse=True)
def _fresh():
    saved = {k: v for k, v in config.get_settings().items() if k in _KEYS + ["announce_defaults"]}
    config.delete_settings_keys(_KEYS)
    yield
    config.delete_settings_keys(_KEYS)
    if saved:
        config.save_settings(saved)


def _c():
    import dashboard
    return TestClient(dashboard.app, client=_LOCAL)


def _app_js():
    return (ROOT / "frontend/js/app.js").read_text(encoding="utf-8")


def _fn(name):
    src = _app_js()
    i = re.search(rf"^    (?:async )?{name}\(", src, re.M).start()
    j = src.index("\n    },\n", i)
    return src[i:j]


def test_asked_sites_are_remembered_and_limited():
    c = _c()
    c.post("/api/settings/preferences", json={"announce_defaults_asked": ["bsky", "tg", "tw", "bsky"]})
    assert c.get("/api/settings/preferences").json()["announce_defaults_asked"] == ["bsky", "tw"]


def test_use_these_defaults_writes_no_defaults():
    """Only Save sends announce_defaults; both buttons mark the site as asked."""
    card = _fn("_postingDefaultsCard")
    assert "announce_defaults_asked" in card
    assert card.index("if (save) {") < card.index("body.announce_defaults =")
    assert card.count("body.announce_defaults =") == 1
    assert "close(false)" in card and "close(true)" in card


def test_the_card_offers_the_same_options_as_settings():
    card = _fn("_postingDefaultsCard")
    assert "this.ANNOUNCE_DEFAULTS[site]" in card and "spec.opts" in card
    assert "'sensitive'" in card and "ageLocked" in card            # hidden for under-18s


def test_the_card_is_offered_once():
    assert "asked.includes(site)) return" in _fn("maybePostingDefaults")


def test_offers_count_as_answered(monkeypatch):
    import pb_import
    import posting.scheduler as sch
    from routes import dashboard_auth as da
    monkeypatch.setattr(sch, "detect_runtime_mode", lambda: "desktop")
    monkeypatch.setattr(pb_import, "present", lambda: True)
    monkeypatch.setattr(config, "is_dashboard_auth_required", lambda: False)
    a = _c().get("/api/auth/signup-status").json()["answered"]
    assert a["postybirb"] is False and a["twofa"] is False
    config.save_settings({"pb_import_asked": True})
    _c().post("/api/settings/preferences", json={"twofa_offered": True})
    a = _c().get("/api/auth/signup-status").json()["answered"]
    assert a["postybirb"] and a["twofa"]
    # No PostyBirb on this computer, or a server: never asked.
    config.delete_settings_keys(["pb_import_asked"])
    monkeypatch.setattr(pb_import, "present", lambda: False)
    assert _c().get("/api/auth/signup-status").json()["answered"]["postybirb"]
    assert da  # imported for the monkeypatch target's module to be loaded


def test_the_twofa_screen_uses_the_existing_endpoints():
    src = _app_js()
    i = src.index("if (screen === 'twofa')")
    body = src[i:src.index("// screen === 'tech'", i)]
    assert "API.totpSetup()" in body and "API.totpEnable(" in body and "twofa_offered: true" in body
