"""Spec 020 — the Platforms page: what needs you first, and only the sites you use.

The hub fired one summary request per platform (24), drew every tile equally loud, showed a
broken login as a small dot and badged half the sites "poll only" from a hand-written flag
that had gone out of date. Now one call returns every platform's role (from what the code
can actually do), headline number in its own word, works, a 30-day daily series and its
health, plus the attention items, each with one fix.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import config
from database import platform_metrics
from posting import manager, post_publisher
from routes import api


def _ts(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")


def _bsky(conn, sid, likes_by_day: dict[int, int]):
    """A Bluesky post with snapshots: {days_ago: likes}."""
    latest = likes_by_day[min(likes_by_day)]
    conn.execute("INSERT INTO bsky_submissions (submission_id, likes) VALUES (?, ?)", (sid, latest))
    for d, v in likes_by_day.items():
        conn.execute("INSERT INTO bsky_snapshots (submission_id, polled_at, likes) VALUES (?, ?, ?)",
                     (sid, _ts(d), v))
    conn.commit()


def _entry(out, code):
    return next(p for p in out["platforms"] if p["code"] == code)


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(api.router)
    return TestClient(app)


@pytest.fixture(autouse=True)
def _fresh_series_cache():
    api._series_cache.clear()
    yield
    api._series_cache.clear()


class TestOneCall:
    def test_every_platform_comes_back_with_its_parts(self, db_conn):
        out = api.platforms_overview()
        codes = {p["code"] for p in out["platforms"]}
        assert set(platform_metrics.ALL_CODES) <= codes
        assert "pod" in codes, "the post-only podcast feed is a platform too"
        p = _entry(out, "bsky")
        for key in ("configured", "role", "headline", "works", "series", "change_pct", "health", "paused"):
            assert key in p, key
        assert p["headline"]["label"] == "Likes"
        assert "summary" in out and "attention" in out

    def test_headline_and_works_come_from_the_platforms_rows(self, db_conn):
        _bsky(db_conn, "at://a/1", {1: 30})
        _bsky(db_conn, "at://a/2", {1: 12})
        p = _entry(api.platforms_overview(), "bsky")
        assert p["works"] == 2 and p["headline"]["value"] == 42

    def test_a_failing_platform_does_not_break_the_rest(self, db_conn, monkeypatch):
        real = api._platform_numbers

        def boom(conn, code):
            if code == "fa":
                raise RuntimeError("table gone")
            return real(conn, code)
        monkeypatch.setattr(api, "_platform_numbers", boom)
        out = api.platforms_overview()
        assert _entry(out, "fa")["error"]
        assert _entry(out, "bsky").get("error") in (None, "")


class TestTheTrend:
    def test_a_30_day_daily_series_carries_values_forward(self, db_conn):
        # likes climb 10 → 40 over three weeks; a day without a poll keeps yesterday's value
        _bsky(db_conn, "at://t/1", {21: 10, 14: 20, 7: 30, 1: 40})
        p = _entry(api.platforms_overview(), "bsky")
        assert len(p["series"]) == 30
        assert p["series"][0]["value"] is None, "nothing before the first snapshot"
        vals = [d["value"] for d in p["series"] if d["value"] is not None]
        assert vals == sorted(vals), "a gap never dips the line"
        assert vals[-1] == 40
        assert p["change_pct"] == pytest.approx(100.0 * (40 - 10) / 10, abs=0.1)

    def test_change_is_none_with_under_a_week_of_history(self, db_conn):
        _bsky(db_conn, "at://n/1", {3: 5, 1: 9})
        assert _entry(api.platforms_overview(), "bsky")["change_pct"] is None

    def test_no_snapshots_means_no_change_never_zero(self, db_conn):
        p = _entry(api.platforms_overview(), "tw")
        assert p["change_pct"] is None


class TestRoles:
    def test_roles_come_from_real_capability(self, db_conn):
        out = api.platforms_overview()
        role = {p["code"]: p["role"] for p in out["platforms"]}
        assert role["bsky"] == "polls + posts", "Bluesky has a poster"
        assert role["mast"] == "polls + posts", "Mastodon posts through the Posts publisher"
        assert role["wp"] == "polls only"
        assert role["pod"] == "posts only"
        assert role["tg"] == "posts + reactions"

    def test_the_work_poster_list_matches_the_factory(self):
        for code in manager.WORK_POSTERS:
            assert manager._poster_class_known(code), code
        assert not manager._poster_class_known("wp")
        assert set(post_publisher.SUPPORTED) >= {"bsky", "mast", "thr", "tum", "tw", "ig", "tg"}

    def test_the_hand_written_flag_is_gone(self):
        js = open("frontend/js/platforms.js", encoding="utf-8").read()
        assert "pollOnly" not in js
        assert "poll only" not in open("frontend/js/platforms_hub.js", encoding="utf-8").read()


class TestAttention:
    def _health(self, **kw):
        h = {"configured": True, "last_poll_status": "success", "last_poll_error": None,
             "throttled_until": None, "session": None}
        h.update(kw)
        return h

    def test_an_expired_login_says_reconnect(self):
        items = api._attention_for("fa", "FurAffinity", self._health(session={"status": "expired"}), None)
        assert items[0]["action"] == "reconnect" and items[0]["level"] == "error"

    def test_a_login_about_to_expire(self):
        cred = {"code": "fa", "age_days": 43, "ttl_days": 45, "level": "aging"}
        items = api._attention_for("fa", "FurAffinity", self._health(), cred)
        assert items and items[0]["action"] == "reconnect" and "2 days" in items[0]["title"]

    def test_a_failed_poll_offers_poll_now(self):
        items = api._attention_for("ws", "Weasyl", self._health(last_poll_status="error",
                                                                last_poll_error="HTTP 500"), None)
        assert items[0]["action"] == "poll" and "HTTP 500" in items[0]["detail"]

    def test_unreachable_offers_details_and_pause(self):
        items = api._attention_for("fn", "FurryNetwork", self._health(
            last_poll_status="error", last_poll_error="403 cf-mitigated: challenge"), None)
        assert items[0]["action"] == "pause" and "reach" in items[0]["title"]

    def test_throttled_says_when_it_resumes(self):
        until = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        items = api._attention_for("ao3", "AO3", self._health(throttled_until=until), None)
        assert items[0]["action"] == "wait" and items[0]["until"] == until

    def test_healthy_or_unconfigured_needs_nothing(self):
        assert api._attention_for("ws", "Weasyl", self._health(), None) == []
        assert api._attention_for("ws", "Weasyl", self._health(configured=False,
                                                               last_poll_status="error"), None) == []


class TestPreferences:
    def test_platform_order_keeps_known_codes_only(self, client):
        good = list(platform_metrics.ALL_CODES)[:3]
        client.post("/api/settings/preferences", json={"platform_order": good + ["nope", good[0]]})
        assert client.get("/api/settings/preferences").json()["platform_order"] == good

    def test_paused_and_hidden_ride_along_in_the_overview(self, db_conn):
        config.save_settings({"polling_paused_platforms": ["fa"], "hidden_platforms": ["pix"]})
        out = api.platforms_overview()
        assert _entry(out, "fa")["paused"] is True
        assert out["hidden"] == ["pix"]

    def test_hiding_with_keep_checking_off_pauses_and_showing_resumes(self):
        """The hide prompt's "stop checking" uses the existing per-platform pause, which the
        server cycle (server.py) and the desktop pollers (is_paused) already skip."""
        from polling import desktop_pollers
        api.pause_platform_polling("fa")
        assert desktop_pollers.is_paused(config.get_settings(), "fa")
        api.resume_platform_polling("fa")
        assert not desktop_pollers.is_paused(config.get_settings(), "fa")
        server_src = open("server.py", encoding="utf-8").read()
        assert "polling_paused_platforms" in server_src


class TestThePage:
    HUB = open("frontend/js/platforms_hub.js", encoding="utf-8").read()
    PICKER = open("frontend/js/platform_picker.js", encoding="utf-8").read()

    def test_it_honours_platforms_i_use(self):
        assert "window.visiblePlatforms" in self.HUB

    def test_one_request_instead_of_one_per_site(self):
        assert "API.getPlatformsOverview()" in self.HUB
        app = open("frontend/js/app.js", encoding="utf-8").read()
        body = app[app.index("async renderPlatformsHub() {"):][:600]
        assert "getFASummary" not in body and "PlatformsHub.render(" in body

    def test_status_is_words_beside_every_dot(self):
        assert "ph-dot" in self.HUB and "ph-st" in self.HUB

    def test_hiding_a_connected_site_asks_about_checking(self):
        assert "Keep checking it for new stats in the background" in self.PICKER
        assert "data-keep checked" in self.PICKER, "ticked by default"
        assert "API.pausePlatformPolling(" in self.PICKER and "API.resumePlatformPolling(" in self.PICKER

    def test_the_panel_is_a_dialog(self):
        assert 'role="dialog"' in self.PICKER and 'aria-modal="true"' in self.PICKER
        assert "'Escape'" in self.PICKER


def test_poll_errors_are_scrubbed_before_they_reach_the_page(db_conn, monkeypatch):
    """4.54.0 release review: a token inside a poll error never reaches the API or the page."""
    import log_redaction
    from database import ws_queries
    monkeypatch.setattr(log_redaction, "_SECRETS", ["sekrit-token-123"])
    monkeypatch.setattr(ws_queries, "get_ws_last_poll",
                        lambda conn: {"started_at": "2026-10-02 00:00:00", "status": "error",
                                      "error_message": "GET https://x.example/bot?key=sekrit-token-123 failed"})
    h = api._health_snapshot()["ws"]
    assert "sekrit-token-123" not in (h["last_poll_error"] or "")
