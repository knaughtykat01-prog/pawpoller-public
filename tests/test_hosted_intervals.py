"""4.74.0 (spec 039): hosted seats poll at one of the box's intervals; everyone else is unchanged."""
from __future__ import annotations

import pytest

import config


@pytest.fixture
def hosted(monkeypatch):
    monkeypatch.setenv("PAWPOLLER_HOSTED", "1")
    monkeypatch.delenv("PAWPOLLER_HOSTED_INTERVALS", raising=False)


@pytest.mark.parametrize("given,want", [(15, 240), (240, 240), (241, 360), (600, 720), (1440, 1440),
                                        (5000, 1440), ("30", 240), (None, 240), ("junk", 240)])
def test_rounding_up_to_the_list(hosted, given, want):
    assert config.allowed_interval(given) == want


def test_not_hosted_is_untouched(monkeypatch):
    monkeypatch.delenv("PAWPOLLER_HOSTED", raising=False)
    monkeypatch.setenv("PAWPOLLER_HOSTED_INTERVALS", "360")
    assert config.hosted_intervals() is None and config.allowed_interval(15) == 15
    config.save_settings({"poll_interval_minutes": 15, "fa_poll_interval_minutes": 30})
    s = config.get_settings()
    assert (s["poll_interval_minutes"], s["fa_poll_interval_minutes"]) == (15, 30)


def test_the_box_sets_the_list(monkeypatch, hosted):
    monkeypatch.setenv("PAWPOLLER_HOSTED_INTERVALS", "1440, 360,480,720")
    assert config.hosted_intervals() == [360, 480, 720, 1440] and config.allowed_interval(240) == 360
    monkeypatch.setenv("PAWPOLLER_HOSTED_INTERVALS", "abc")
    assert config.hosted_intervals() == list(config.HOSTED_INTERVALS_DEFAULT)
    monkeypatch.setenv("PAWPOLLER_HOSTED_INTERVALS", "5,10")      # under 15 min: ignored, so the default
    assert config.hosted_intervals() == list(config.HOSTED_INTERVALS_DEFAULT)


def test_save_rounds_and_leaves_a_notice_once(hosted):
    config.save_settings({"poll_interval_minutes": 30, "fa_poll_interval_minutes": 60})
    s = config.get_settings()
    assert (s["poll_interval_minutes"], s["fa_poll_interval_minutes"]) == (240, 240)
    assert s["interval_changed_from"] == 30
    config.save_settings({"poll_interval_minutes": 480})              # a clean save clears the notice
    assert "interval_changed_from" not in config.get_settings()
    config.save_settings({"display_timezone": "UTC"})                 # unrelated saves leave it alone
    assert config.get_settings()["poll_interval_minutes"] == 480


def test_a_move_in_is_rounded(hosted):
    full = {**config.get_settings(), "poll_interval_minutes": 15, "tw_poll_interval_minutes": 15}
    config.replace_settings(full)
    s = config.get_settings()
    assert (s["poll_interval_minutes"], s["tw_poll_interval_minutes"], s["interval_changed_from"]) == (240, 240, 15)


def test_the_save_route_accepts_24_hours_on_a_seat(hosted):
    from fastapi.testclient import TestClient
    import dashboard
    c = TestClient(dashboard.app)
    assert c.post("/api/settings/preferences", json={"poll_interval_minutes": 1440}).status_code == 200
    assert config.get_settings()["poll_interval_minutes"] == 1440
    c.post("/api/settings/preferences", json={"poll_interval_minutes": 30})
    assert config.get_settings()["poll_interval_minutes"] == 240
    prefs = c.get("/api/settings/preferences").json()
    assert prefs["interval_choices"] == [240, 360, 480, 720, 1440] and prefs["interval_changed_from"] == 30


def test_desktop_route_still_refuses_odd_values(monkeypatch):
    from fastapi.testclient import TestClient
    import dashboard
    monkeypatch.delenv("PAWPOLLER_HOSTED", raising=False)
    config.save_settings({"poll_interval_minutes": 240})
    c = TestClient(dashboard.app)
    c.post("/api/settings/preferences", json={"poll_interval_minutes": 1440})
    assert config.get_settings()["poll_interval_minutes"] == 240
    assert c.get("/api/settings/preferences").json()["interval_choices"] is None


def test_text_values_are_not_a_change(hosted):
    config.save_settings({"poll_interval_minutes": "360"})
    s = config.get_settings()
    assert s["poll_interval_minutes"] == 360 and "interval_changed_from" not in s


def test_odd_values_never_break_a_save(hosted):
    assert config.allowed_interval(float("inf")) == 240
    config.save_settings({"poll_interval_minutes": float("inf")})
    assert config.get_settings()["poll_interval_minutes"] == 240
    from fastapi.testclient import TestClient
    import dashboard
    assert TestClient(dashboard.app).post("/api/settings/preferences",
                                          json={"poll_interval_minutes": "soon"}).status_code == 200
