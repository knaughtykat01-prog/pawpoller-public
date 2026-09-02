"""Unit tests for the active session-validity checker (polling/session_check).

Covers the credential gates, the unconfigured short-circuit (which must NOT
build a client or touch the network), and the problem-summary filter used by
the banner/notification layer. The network-calling paths (_validate) are not
exercised here — they're integration-tested implicitly via the connect routes.
"""
import asyncio

from polling import session_check as sc


def test_configured_gates():
    assert sc._configured("ao3", {"ao3_session_cookie": "abc"}) is True
    assert sc._configured("ao3", {"ao3_username": "u", "ao3_password": "p"}) is True
    assert sc._configured("ao3", {}) is False
    assert sc._configured("bsky", {"bsky_identifier": "x", "bsky_app_password": "y"}) is True
    assert sc._configured("bsky", {"bsky_identifier": "x"}) is False   # half-credentials
    assert sc._configured("mast", {"mast_instance_url": "u", "mast_access_token": "t"}) is True
    assert sc._configured("pix", {"pix_refresh_token": "t"}) is True
    assert sc._configured("thr", {"thr_access_token": "t"}) is True
    assert sc._configured("thr", {}) is False
    assert sc._configured("ig", {"ig_access_token": "t"}) is True
    assert sc._configured("ig", {}) is False


def test_check_platform_unconfigured_skips_network():
    # Empty settings → 'unconfigured'; no client is built, no request made.
    entry = asyncio.run(sc.check_platform("ao3", {}))
    assert entry["status"] == "unconfigured"
    assert entry["detail"] is None
    assert "checked_at" in entry
    # Cached under the platform code.
    assert sc.get_session_health()["ao3"]["status"] == "unconfigured"


def test_summarize_problems_filters_healthy():
    snap = {
        "ao3": {"status": "expired", "detail": "dead cookie"},
        "sf": {"status": "valid", "detail": None},
        "bsky": {"status": "error", "detail": "timeout"},
        "pix": {"status": "unconfigured", "detail": None},
    }
    problems = sc.summarize_problems(snap)
    assert {p["code"] for p in problems} == {"ao3", "bsky"}   # only expired + error
    for p in problems:
        assert p["label"] and p["status"] in ("expired", "error")
    # AO3 gets its friendly label.
    assert next(p for p in problems if p["code"] == "ao3")["label"] == "AO3"


def test_all_checkable_have_configured_gates():
    # Every CHECKABLE platform must return a bool from _configured (no KeyError).
    for code in sc.CHECKABLE:
        assert sc._configured(code, {}) is False


def test_valid_check_autoclears_a_mute(monkeypatch):
    # A muted platform that validates again has its mute auto-cleared, so a
    # future failure re-alerts ("mute until fixed", not "forever"). Only the
    # recovered platform is cleared — others stay muted.
    import config

    async def _fake_validate(code, s):
        return "someuser"                      # truthy → 'valid'
    monkeypatch.setattr(sc, "_validate", _fake_validate)

    config.save_settings({"muted_session_codes": ["bsky", "thr"]})
    s = {"bsky_identifier": "x", "bsky_app_password": "y",
         "muted_session_codes": ["bsky", "thr"]}
    entry = asyncio.run(sc.check_platform("bsky", s))
    assert entry["status"] == "valid"
    remaining = config.get_settings().get("muted_session_codes") or []
    assert "bsky" not in remaining and "thr" in remaining
    config.save_settings({"muted_session_codes": []})   # cleanup
