"""Proactive credential-age tracking (backlog W, 2.170.0).

Cookie/token logins that expire without auto-refresh (X/FA/DA) get a heads-up
before they go stale. These cover the two seams: save_settings stamping a
(re)connect, and the age→level computation the UI reads.
"""
from datetime import datetime, timedelta, timezone

import config


def _ago(days):
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")


def test_stamp_on_credential_change():
    config.save_settings({"tw_auth_token": "abc", "tw_ct0": "xyz"})
    stamps = config.get_settings().get("credential_set_at") or {}
    assert "tw" in stamps


def test_no_stamp_for_noncredential_save():
    config.save_settings({"some_pref": 1})
    stamps = config.get_settings().get("credential_set_at") or {}
    assert "tw" not in stamps and "fa" not in stamps


def test_no_stamp_when_value_unchanged():
    config.save_settings({"tw_auth_token": "abc"})
    first = (config.get_settings().get("credential_set_at") or {}).get("tw")
    # Saving the SAME value again is not a reconnect → stamp unchanged.
    config.save_settings({"tw_auth_token": "abc"})
    second = (config.get_settings().get("credential_set_at") or {}).get("tw")
    assert first == second


def test_report_only_lists_configured():
    config.save_settings({"tw_auth_token": "abc"})   # tw configured; fa/da not
    codes = {r["code"] for r in config.credential_age_report()}
    assert codes == {"tw"}


# Note: saving a credential stamps set_at=now (a reconnect is fresh), which is
# the point — so these set the credential first, then age the stamp in a second,
# credential-free save (which must NOT re-stamp).

def test_level_ok_when_recent():
    config.save_settings({"tw_auth_token": "abc"})
    config.save_settings({"credential_set_at": {"tw": _ago(2)}})
    rep = {r["code"]: r for r in config.credential_age_report()}
    assert rep["tw"]["level"] == "ok"
    assert rep["tw"]["age_days"] == 2


def test_level_aging_near_ttl():
    # tw ttl = 30; 24 days = 0.8 → aging
    config.save_settings({"tw_auth_token": "abc"})
    config.save_settings({"credential_set_at": {"tw": _ago(24)}})
    rep = {r["code"]: r for r in config.credential_age_report()}
    assert rep["tw"]["level"] == "aging"


def test_level_stale_past_ttl():
    config.save_settings({"fa_cookie_a": "c"})           # ttl 45
    config.save_settings({"credential_set_at": {"fa": _ago(60)}})
    rep = {r["code"]: r for r in config.credential_age_report()}
    assert rep["fa"]["level"] == "stale"


def test_backfill_stamps_configured_but_unstamped():
    config.save_settings({"fa_cookie_a": "c"})          # this stamps fa
    config.save_settings({"credential_set_at": {}})     # simulate a pre-feature install
    assert config.backfill_credential_stamps() is True
    assert "fa" in (config.get_settings().get("credential_set_at") or {})
    # Idempotent second call writes nothing.
    assert config.backfill_credential_stamps() is False
