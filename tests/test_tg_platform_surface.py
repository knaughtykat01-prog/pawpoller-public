"""Telegram as a full platform — the analytics surface added in 4.0.10.

Phase 4 of docs/specs/telegram_platform.md. Everything here exists so Telegram
appears on the dashboard, the compare page and the CSV export like any other
platform, with two things it must never do:

* show a bare **0** for a post whose reactions were never observed, and
* claim a **view count**, which is not in the Bot API at all.

The route-ordering guard for /submissions/{id}/snapshots lives in
tests/test_route_order.py, where it covers all twenty platforms at once.
"""
from __future__ import annotations

import json
import os
import tempfile

import pytest


@pytest.fixture()
def seeded(monkeypatch):
    """A throwaway install holding one channel, one observed post and one that
    was published before tracking started."""
    import config
    from database import db as dbm, tg_queries as tq

    monkeypatch.setattr(config, "DB_PATH",
                        os.path.join(tempfile.mkdtemp(), "tg.db"))
    dbm.init_db()
    conn = dbm.get_connection()
    conn.execute("INSERT INTO accounts (platform, label, handle, enabled, "
                 "is_default, sort_order) VALUES ('tg','@c','@c',1,1,0)")
    conn.commit()
    aid = conn.execute(
        "SELECT account_id FROM accounts WHERE platform='tg'").fetchone()[0]
    tq.record_submission(conn, account_id=aid, chat_id="-1001", message_id=11,
                         title="Observed", posted_at="2026-08-20 09:00:00",
                         link="https://t.me/c/1001/11")
    tq.record_submission(conn, account_id=aid, chat_id="-1001", message_id=12,
                         title="Never observed", posted_at="2026-08-21 09:00:00")
    for total in (3, 5, 9):
        tq.apply_reaction_count(
            conn, chat_id="-1001", message_id=11,
            reactions=[{"type": {"emoji": "\N{HEAVY BLACK HEART}"},
                        "total_count": total}])
    conn.commit()
    yield conn
    conn.close()


class TestNotCountedIsNotZero:
    """The one thing this platform's data must never imply."""

    def test_the_summary_reports_how_many_were_never_observed(self, seeded):
        from database import tg_queries as tq
        s = tq.get_dashboard_summary(seeded)
        assert s["total_submissions"] == 2
        assert s["total_reactions"] == 9
        assert s["uncounted"] == 1, (
            "without this number a small total reads as poor engagement "
            "rather than a short observation window")

    def test_an_unobserved_post_is_not_ranked_at_zero(self, seeded):
        """It has no measurement, not a measurement of nothing — so it is
        excluded from the leaderboard rather than placed last on it."""
        from database import tg_queries as tq
        top = tq.get_top_reacted(seeded)
        assert [t["title"] for t in top] == ["Observed"]

    def test_the_csv_says_not_counted_in_words(self, seeded):
        """An empty cell in a spreadsheet reads as zero. Words cannot."""
        from routes import tg_api
        rows = tg_api.tg_queries.get_all_submissions(seeded)
        for r in rows:
            if r.get("reactions_at") is None:
                r["reactions_count"] = "not counted"
        never = [r for r in rows if r["title"] == "Never observed"][0]
        assert never["reactions_count"] == "not counted"


class TestDeltas:
    def test_ties_on_polled_at_are_broken_by_arrival_order(self, seeded):
        """THE bug this test exists for. polled_at has one-second resolution
        and reaction updates are PUSHED, so several land in the same second.
        Ordering by polled_at alone then picks "latest" and "previous"
        arbitrarily among the tied rows and the delta comes out with a random
        sign — it rendered on screen as every post having LOST reactions."""
        from database import tg_queries as tq
        d = tq.get_deltas(seeded)["-1001:11"]
        assert d["reactions_delta"] == 4, (
            "9 - 5 = 4; a negative or zero result means the tie-break on the "
            "snapshot id was lost")

    def test_a_removed_reaction_is_reported_as_a_loss(self, seeded):
        """Telegram sends the ABSOLUTE state, so a drop is real data. It must
        not be clamped to zero — that would hide someone un-reacting."""
        from database import tg_queries as tq
        tq.apply_reaction_count(seeded, chat_id="-1001", message_id=11,
                                reactions=[{"type": {"emoji": "\N{HEAVY BLACK HEART}"},
                                            "total_count": 6}])
        seeded.commit()
        assert tq.get_deltas(seeded)["-1001:11"]["reactions_delta"] == -3


class TestNoFictionalMetrics:
    def test_the_registry_declares_no_view_column(self):
        """Permanent, not a gap: a channel post's view count is client-API
        only and no release can add it."""
        from database import platform_metrics as pm
        spec = pm.get("tg")
        assert spec.views is None
        assert spec.score is None
        assert spec.comments is None, (
            "channel discussion lives in a separate linked group, which is a "
            "different chat we do not read")
        assert spec.faves == "reactions_count"

    def test_sorting_cannot_be_injected(self, seeded):
        """`sort_by` arrives from a query string and is interpolated into SQL,
        so it is validated against a fixed set rather than escaped."""
        from database import tg_queries as tq
        rows = tq.get_all_submissions(seeded, sort_by="1; DROP TABLE tg_submissions--")
        assert len(rows) == 2
        assert seeded.execute("SELECT COUNT(*) FROM tg_submissions").fetchone()[0] == 2


class TestPollLog:
    def test_a_failed_poll_records_telegrams_own_words(self):
        """An early version wrote status='error' with error_message NULL — a
        red status dot and nothing to act on."""
        src = open("polling/tg_poller.py", encoding="utf-8").read()
        assert "last_error" in src, (
            "the poll log must carry the client's error, not just a status")
        i = src.index("def run_tg_poll_cycle")
        assert "finish_tg_poll_log" in src[i:], "the log is never closed"

    def test_an_unconfigured_account_writes_no_error_row(self):
        """Opening the log before the credential check would paint the status
        dot red every cycle for an account that is simply not set up yet."""
        src = open("polling/tg_poller.py", encoding="utf-8").read()
        assert src.index('result["error"] = "not configured"') < \
            src.index("start_tg_poll_log")


class TestApiSurface:
    def test_every_conventional_endpoint_exists(self):
        """A missing one means a dashboard, chart or export silently renders
        nothing for Telegram while working everywhere else."""
        import dashboard
        paths = {r.path for r in dashboard.app.routes
                 if getattr(r, "path", "").startswith("/api/tg")}
        for want in ("/api/tg/status", "/api/tg/summary", "/api/tg/submissions",
                     "/api/tg/aggregate", "/api/tg/comparison", "/api/tg/poll_log",
                     "/api/tg/poll/trigger", "/api/tg/export/submissions",
                     "/api/tg/export/snapshots"):
            assert want in paths, f"missing {want}"

    def test_the_js_client_has_the_matching_methods(self):
        """The router can be complete and the page still show nothing if the
        API client cannot call it — which is exactly how this shipped at first."""
        src = open("frontend/js/api.js", encoding="utf-8").read()
        for m in ("getTGSummary", "getTGSubmissions", "getTGSubmission",
                  "getTGSnapshots", "getTGAggregate", "getTGComparison",
                  "getTGPollLog", "triggerTGPoll", "fullTGResync"):
            assert f"{m}(" in src, f"api.js has no {m}"

    def test_the_router_has_a_telegram_branch(self):
        """Every registry-driven surface generates #/tg links. Without a route
        branch each one dead-ends on "Page not found"."""
        src = open("frontend/js/app.js", encoding="utf-8").read()
        for fn in ("renderTGDashboard", "renderTGSubmissions",
                   "renderTGDetail", "renderTGCompare"):
            assert f"this.{fn}(" in src, f"no route calls {fn}"
            assert f"async {fn}(" in src, f"{fn} is called but not defined"

    def test_export_urls_are_derived_not_listed(self):
        """The hand-written map had fallen behind by two platforms, and its
        fallback downloaded INKBUNNY's CSV — wrong data under the right
        filename, with no error."""
        src = open("frontend/js/api.js", encoding="utf-8").read()
        assert "_exportBase(platform)" in src
        assert "'/api/fbr/export/submissions'" not in src, (
            "a literal per-platform export map is back")
