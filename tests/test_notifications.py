"""Tests for the notification-centre endpoints (routes/api.py).

Covers timestamp normalisation across the two on-disk formats, the feed
shape + per-item unread flags, mark-read clearing unread, and session-expiry
events being merged into the feed.
"""
import routes.api as api


def test_norm_ts_collapses_formats():
    a = api._norm_ts("2026-07-05 09:24:31")           # poll/posting format
    b = api._norm_ts("2026-07-05T09:24:31.569+00:00")  # session/last_read format
    assert a == b == "2026-07-05 09:24:31"
    assert api._norm_ts(None) == ""


def test_notifications_shape_and_per_item_unread():
    r = api.get_notifications(10)
    assert set(r.keys()) == {"items", "unread", "last_read_at"}
    assert isinstance(r["items"], list) and isinstance(r["unread"], int)
    for it in r["items"]:
        assert isinstance(it.get("unread"), bool)
        assert "timestamp" in it and "summary" in it and "status" in it
    # Count matches the per-item flags.
    assert r["unread"] == sum(1 for it in r["items"] if it["unread"])


def test_mark_read_clears_unread():
    # Marking read sets the server-side marker to now; a quiescent feed
    # (all events in the past) then reports zero unread.
    api.mark_notifications_read()
    r = api.get_notifications(10)
    assert r["unread"] == 0
    assert all(it["unread"] is False for it in r["items"])


def test_clear_hides_past_events():
    # Clearing sets a watermark at 'now'; a quiescent feed (all past events)
    # then returns nothing and reports zero unread.
    api.clear_notifications()
    r = api.get_notifications(10)
    assert r["items"] == []
    assert r["unread"] == 0


def test_session_expiry_merged_into_feed(monkeypatch):
    from polling import session_check as sc
    monkeypatch.setattr(sc, "_session_health", {
        "ao3": {"status": "expired", "detail": "dead cookie",
                "checked_at": "2099-01-01T00:00:00+00:00"},
        "sf": {"status": "valid", "detail": None, "checked_at": "2099-01-01T00:00:00+00:00"},
    })
    r = api.get_notifications(30)
    session_items = [it for it in r["items"] if it.get("kind") == "session"]
    # Only the expired one surfaces (valid is healthy → omitted).
    assert len(session_items) == 1
    assert "AO3" in session_items[0]["summary"] and "expired" in session_items[0]["summary"]
    assert session_items[0]["status"] == "error"


def test_muted_session_alert_is_quiet(monkeypatch):
    """A muted session alert stays in the feed but reports muted=True and never
    counts toward unread; unmuting makes it count again."""
    import config
    from polling import session_check as sc
    monkeypatch.setattr(sc, "_session_health", {
        "thr": {"status": "error", "detail": "Meta blocked API access",
                "checked_at": "2099-01-01T00:00:00+00:00"},
    })
    # last_read in the past so the (future-dated) alert would be unread if unmuted.
    config.save_settings({"muted_session_codes": [],
                          "notifications_last_read_at": "2000-01-01T00:00:00+00:00"})

    def _thr(resp):
        return [it for it in resp["items"] if it.get("kind") == "session" and it["platform"] == "thr"]

    r = api.get_notifications(30)
    assert len(_thr(r)) == 1 and _thr(r)[0]["muted"] is False and _thr(r)[0]["unread"] is True

    config.save_settings({"muted_session_codes": ["thr"]})
    r2 = api.get_notifications(30)
    assert len(_thr(r2)) == 1                      # still visible in the feed
    assert _thr(r2)[0]["muted"] is True
    assert _thr(r2)[0]["unread"] is False          # muted → quiet, no unread
    config.save_settings({"muted_session_codes": []})   # cleanup


def test_mute_endpoint_add_remove_and_reject_unknown():
    import config
    import pytest
    from fastapi import HTTPException
    config.save_settings({"muted_session_codes": []})
    r = api.mute_session_alert({"code": "ig", "muted": True})
    assert "ig" in r["muted_session_codes"]
    r = api.mute_session_alert({"code": "ig", "muted": True})          # idempotent add
    assert r["muted_session_codes"].count("ig") == 1
    r = api.mute_session_alert({"code": "ig", "muted": False})
    assert "ig" not in r["muted_session_codes"]
    with pytest.raises(HTTPException):
        api.mute_session_alert({"code": "not-a-platform", "muted": True})
    config.save_settings({"muted_session_codes": []})   # cleanup
