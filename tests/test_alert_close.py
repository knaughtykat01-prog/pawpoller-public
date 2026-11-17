"""Spec 089: closing a Needs attention alert on Platforms.

Closing hides one card. It is remembered against the alert's kind and reason, recorded by the
install itself, so a problem that changes, or clears and comes back, shows again; and a closed
sign-in alert returns after any failed post to that site. Nothing else changes: the "need you"
count and the site's own status stay as they were.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import config
from routes import api


def _health(**over) -> dict:
    return {"configured": True, "session": {}, "last_poll_status": "ok", "last_poll_error": None,
            "last_poll_at": None, "next_poll_at": None, "throttled_until": None, **over}


@pytest.fixture
def world(db_conn, monkeypatch):
    """Two sites with a problem each, driven by a dict the test can change."""
    state = {
        "health": {"fa": _health(), "ws": _health(last_poll_status="error", last_poll_error="HTTP 500 from the site")},
        "creds": {"fa": {"code": "fa", "age_days": 50, "ttl_days": 45}},
    }
    monkeypatch.setattr(api, "_health_snapshot", lambda settings=None: state["health"])
    monkeypatch.setattr(config, "credential_age_report",
                        lambda settings=None: list(state["creds"].values()))
    config.save_settings({"alerts_closed": {}})
    api._series_cache.clear()
    yield state
    config.delete_settings_keys(["alerts_closed"])


def _alerts() -> dict:
    return {(a["code"], a["kind"]): a for a in api.platforms_overview()["attention"]}


def _close(*pairs, **body):
    return api.close_attention({"items": [{"code": c, "kind": k} for c, k in pairs], **body})


def test_every_alert_has_a_kind():
    until = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    cases = [
        (_health(session={"status": "expired"}), None, "signed_out"),
        (_health(), {"age_days": 50, "ttl_days": 45}, "login_expired"),
        (_health(), {"age_days": 43, "ttl_days": 45}, "login_expiring"),
        (_health(throttled_until=until), None, "slow_down"),
        (_health(last_poll_status="error", last_poll_error="Cloudflare challenge"), None, "unreachable"),
        (_health(last_poll_status="error", last_poll_error="HTTP 500"), None, "check_failed"),
    ]
    for h, cred, kind in cases:
        kinds = [i["kind"] for i in api._attention_for("fa", "FurAffinity", h, cred)]
        assert kind in kinds, (kind, kinds)


def test_close_folds_and_count_is_unchanged(world):
    before = api.platforms_overview()
    assert _close(("fa", "login_expired")) == {"closed": 1}
    after = api.platforms_overview()
    closed = {(a["code"], a["kind"]): a["closed"] for a in after["attention"]}
    assert closed == {("fa", "login_expired"): True, ("ws", "check_failed"): False}
    assert after["summary"]["need_you"] == before["summary"]["need_you"] == 2, "the problems still exist"
    fa = next(p for p in after["platforms"] if p["code"] == "fa")
    assert fa["attention"][0]["closed"] is True and fa["health"] == before_health(before, "fa")


def before_health(out, code):
    return next(p for p in out["platforms"] if p["code"] == code)["health"]


def test_closed_alert_returns_when_kind_changes(world):
    """A guess ("probably expired") that becomes a fact ("signed out") is a different alert."""
    _close(("fa", "login_expired"))
    world["health"]["fa"] = _health(session={"status": "expired"})
    alerts = _alerts()
    assert ("fa", "signed_out") in alerts and alerts[("fa", "signed_out")]["closed"] is False


def test_expiring_then_expired_shows_again(world):
    world["creds"]["fa"] = {"code": "fa", "age_days": 43, "ttl_days": 45}
    _close(("fa", "login_expiring"))
    assert _alerts()[("fa", "login_expiring")]["closed"] is True
    world["creds"]["fa"] = {"code": "fa", "age_days": 46, "ttl_days": 45}
    assert _alerts()[("fa", "login_expired")]["closed"] is False


def test_day_countdown_alone_does_not_reopen(world):
    world["creds"]["fa"] = {"code": "fa", "age_days": 42, "ttl_days": 45}   # "expires in 3 days"
    _close(("fa", "login_expiring"))
    world["creds"]["fa"] = {"code": "fa", "age_days": 44, "ttl_days": 45}   # "expires in 1 day"
    a = _alerts()[("fa", "login_expiring")]
    assert "1 day" in a["title"] and a["closed"] is True


def test_closed_alert_returns_when_reason_changes(world):
    _close(("ws", "check_failed"))
    assert _alerts()[("ws", "check_failed")]["closed"] is True
    world["health"]["ws"] = _health(last_poll_status="error", last_poll_error="the site sent an empty page")
    assert _alerts()[("ws", "check_failed")]["closed"] is False


def test_a_newer_check_with_the_same_problem_stays_closed(world):
    _close(("ws", "check_failed"))
    world["health"]["ws"] = _health(last_poll_status="error", last_poll_error="HTTP 500 from the site",
                                    last_poll_at="2099-01-01T00:00:00+00:00")
    assert _alerts()[("ws", "check_failed")]["closed"] is True


def test_closed_alert_returns_after_clearing(world):
    _close(("ws", "check_failed"))
    world["health"]["ws"] = _health()                       # fixed
    assert ("ws", "check_failed") not in _alerts()
    assert "ws" not in config.get_settings()["alerts_closed"], "the record went with the problem"
    world["health"]["ws"] = _health(last_poll_status="error", last_poll_error="HTTP 500 from the site")
    assert _alerts()[("ws", "check_failed")]["closed"] is False


def _fail_in_queue(conn):
    from database import posting_queries
    conn.execute("INSERT INTO posting_queue (story_name, chapter_index, platform) VALUES ('Sample_Story', 0, 'fa')")
    qid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    posting_queries.update_queue_status(conn, qid, "failed", error="the site refused the login")
    conn.commit()


def _fail_in_log(conn):
    from database import posting_queries
    posting_queries.log_posting_action(conn, "fa", "Sample_Story", 0, action="post", status="failed",
                                       error_message="the site refused the login")
    conn.commit()


def _fail_a_journal(conn):
    from database import posts_queries
    post_id = posts_queries.create_post(conn, title="Sample journal", body="Words.", now=_later())
    posts_queries.upsert_post_publication(conn, post_id=post_id, platform="fa", account_id=0, status="failed",
                                          external_id="", external_url="", error="the site refused the login",
                                          now=_later())
    conn.commit()


def _later() -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=5)).isoformat()


@pytest.mark.parametrize("fail", [_fail_in_queue, _fail_in_log, _fail_a_journal],
                         ids=["a scheduled post", "a post now", "a journal"])
def test_failed_post_reopens_a_closed_signin_alert(world, db_conn, monkeypatch, fail):
    """Wherever the failure is written. Any failure counts: nothing tells a sign-in failure apart."""
    _close(("fa", "login_expired"), ("ws", "check_failed"))
    closed = config.get_settings()["alerts_closed"]           # as if closed five minutes ago
    past = (datetime.now(timezone.utc) - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
    for by_kind in closed.values():
        for rec in by_kind.values():
            rec["at"] = past
    config.save_settings({"alerts_closed": closed})
    assert _alerts()[("fa", "login_expired")]["closed"] is True
    fail(db_conn)
    alerts = _alerts()
    assert alerts[("fa", "login_expired")]["closed"] is False
    assert alerts[("ws", "check_failed")]["closed"] is True, "another site's alert, and not a sign-in one"


def test_closed_record_holds_no_error_text(world):
    world["health"]["ws"] = _health(last_poll_status="error", last_poll_error="token=SECRET-IN-THE-ERROR at /home/x")
    _close(("ws", "check_failed"))
    stored = repr(config.get_settings()["alerts_closed"])
    assert "SECRET" not in stored and "/home" not in stored
    rec = config.get_settings()["alerts_closed"]["ws"]["check_failed"]
    assert set(rec) == {"r", "at"} and len(rec["r"]) == 12


def test_caller_cannot_supply_alert_state(world):
    """Naming an alert that isn't listed, or sending a state for it, records nothing."""
    api.close_attention({"items": [{"code": "bsky", "kind": "signed_out", "r": "x", "at": "2099-01-01 00:00:00"},
                                   {"code": "fa", "kind": "signed_out"}, {"code": "fa"}, "fa", None]})
    assert config.get_settings()["alerts_closed"] == {}
    api.close_attention({"items": [{"code": "fa", "kind": "login_expired", "r": "ffffffffffff",
                                    "at": "2099-01-01 00:00:00"}]})
    rec = config.get_settings()["alerts_closed"]["fa"]["login_expired"]
    assert rec["r"] == "" and rec["at"] < "2099"


def test_close_all_closes_only_what_is_listed(world):
    assert _close(all=True) == {"closed": 2}
    assert all(a["closed"] for a in _alerts().values())
    assert set(config.get_settings()["alerts_closed"]) == {"fa", "ws"}


def test_closing_one_keeps_the_others_closed(world):
    _close(("fa", "login_expired"))
    _close(("ws", "check_failed"))
    assert all(a["closed"] for a in _alerts().values())


def test_show_brings_everything_back(world):
    _close(all=True)
    assert api.show_attention() == {"closed": 0}
    assert not any(a["closed"] for a in _alerts().values())


def test_over_long_request_is_refused(world):
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as e:
        api.close_attention({"items": [{"code": "fa", "kind": "login_expired"}] * 201})
    assert e.value.status_code == 400


def test_a_paused_site_has_no_alert_to_close(world):
    config.save_settings({"polling_paused_platforms": ["ws"]})
    try:
        assert ("ws", "check_failed") not in _alerts()
    finally:
        config.delete_settings_keys(["polling_paused_platforms"])


def test_a_check_in_progress_does_not_bring_a_closed_alert_back(world):
    """Mid-check the site lists no alert yet. That is not the problem going away (gap check, FR-025)."""
    _close(("ws", "check_failed"))
    world["health"]["ws"] = _health(last_poll_status="running")
    assert ("ws", "check_failed") not in _alerts()
    assert "check_failed" in config.get_settings()["alerts_closed"]["ws"], "the record was kept through the check"
    world["health"]["ws"] = _health(last_poll_status="error", last_poll_error="HTTP 500 from the site")
    assert _alerts()[("ws", "check_failed")]["closed"] is True, "the same problem, unchanged, stays closed"
    world["health"]["ws"] = _health(last_poll_status="running")
    _alerts()
    world["health"]["ws"] = _health(last_poll_status="error", last_poll_error="a different failure")
    assert _alerts()[("ws", "check_failed")]["closed"] is False, "a changed problem still shows"


def test_closed_alerts_do_not_travel_between_a_desktop_and_its_server(world):
    """Each install forgets a record when its OWN alert goes; a synced copy would be pruned against the wrong list."""
    _close(all=True)
    assert "alerts_closed" in config.SYNC_EXCLUDE
    assert "alerts_closed" not in config.get_settings_for_sync()[0]
    for travels in ("announce_closed", "announcements_enabled"):
        assert travels not in config.SYNC_EXCLUDE
