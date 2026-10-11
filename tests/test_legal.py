"""Terms + Privacy acceptance (4.67.0, spec 033 US2)."""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import config
import legal

ROOT = Path(__file__).resolve().parents[1]
_KEYS = ["legal_accepted", "legal_history"]


@pytest.fixture(autouse=True)
def _clean():
    config.delete_settings_keys(_KEYS)
    yield
    config.delete_settings_keys(_KEYS)


@pytest.mark.repo_only
@pytest.mark.skipif(not (ROOT / "site").is_dir(), reason="the marketing site isn't in this copy")
def test_versions_match_the_site():
    """The app shows what the website says: re-run deploy/build_legal.py after a bump."""
    for doc in legal.DOCS:
        astro = (ROOT / "site" / "src" / "pages" / f"{doc}.astro").read_text(encoding="utf-8")
        html = (ROOT / "frontend" / "legal" / f"{doc}.html").read_text(encoding="utf-8")
        assert int(re.search(r"^const version = (\d+);", astro, re.M).group(1)) == \
            int(re.search(r'data-version="(\d+)"', html).group(1)), doc


def test_bundled_documents_are_plain():
    """Only document tags, no scripts or styles from the site build."""
    for doc in legal.DOCS:
        html = (ROOT / "frontend" / "legal" / f"{doc}.html").read_text(encoding="utf-8")
        assert "<script" not in html and "style=" not in html and "class=\"text-" not in html
        assert len(html) > 3000


def test_accept_only_the_current_versions():
    cur = legal.current()
    assert legal.needs_accept()
    with pytest.raises(ValueError):
        legal.accept(cur["terms"] - 1, cur["privacy"])
    rec = legal.accept(cur["terms"], cur["privacy"])
    assert rec["terms"] == cur["terms"] and rec["at"]
    assert not legal.needs_accept()
    # A newer bundled version means asking again.
    assert legal.needs_accept({"legal_accepted": {"terms": cur["terms"] - 1, "privacy": cur["privacy"]}})


def test_history_is_capped():
    cur = legal.current()
    for _ in range(14):
        legal.accept(cur["terms"], cur["privacy"])
    assert len(config.get_settings()["legal_history"]) == 10


def _gated(monkeypatch):
    import dashboard
    monkeypatch.setattr(dashboard, "SIGNUP_GATES", True)
    monkeypatch.setattr(config, "is_dashboard_auth_required", lambda: True)
    real = config.get_settings   # an account with its email (4.68.0's email gate is a separate test)
    monkeypatch.setattr(config, "get_settings", lambda: {**real(), "auth_email": "owner@example.com"})
    c = TestClient(dashboard.app)
    c.cookies.set("pp_session", config.sign_session({"u": "SecondFur"}))
    return c


def test_a_session_waits_for_the_terms(monkeypatch):
    c = _gated(monkeypatch)
    r = c.get("/api/settings/preferences")
    assert r.status_code == 403 and r.json()["error"] == "terms"
    assert c.get("/api/legal/status").status_code == 200
    assert c.get("/api/auth/signup-status").status_code == 200
    cur = legal.current()
    r = c.post("/api/legal/accept", json={"terms_version": cur["terms"], "privacy_version": cur["privacy"]})
    assert r.status_code == 200
    assert c.get("/api/settings/preferences").status_code == 200


def test_stale_accept_is_a_409(monkeypatch):
    c = _gated(monkeypatch)
    assert c.post("/api/legal/accept", json={"terms_version": 0, "privacy_version": 0}).status_code == 409


def test_api_keys_skip_the_terms(monkeypatch):
    """A paired desktop's key: the owner accepted on the install that issued it."""
    import dashboard
    monkeypatch.setattr(dashboard, "SIGNUP_GATES", True)
    monkeypatch.setattr(config, "is_dashboard_auth_required", lambda: True)
    monkeypatch.setattr(config, "validate_api_key", lambda k: k == "pp_test")
    r = TestClient(dashboard.app).get("/api/settings/preferences", headers={"Authorization": "Bearer pp_test"})
    assert r.status_code == 200


def test_the_accept_button_waits_for_the_end():
    """The scroll gate: disabled until the end of both documents has been on screen."""
    src = (ROOT / "frontend" / "js" / "app.js").read_text(encoding="utf-8")
    legal_js = src[src.index("if (screen === 'legal')"):src.index("if (screen === 'age')")]
    assert 'id="legal-accept" disabled aria-describedby="legal-reason"' in legal_js
    assert "box.scrollHeight - box.scrollTop - box.clientHeight" in legal_js
    assert "if (!reached) return;" in legal_js
    assert 'tabindex="0"' in legal_js and 'aria-live="polite"' in legal_js
