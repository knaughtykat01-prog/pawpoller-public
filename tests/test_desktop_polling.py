"""The desktop polls every platform the registry knows (4.3.2).

docs/specs/desktop_polling.md. ``main.py`` started polling from sixteen
hand-written near-identical thread functions and the list stopped at ``ig``, so
e621, FurryNetwork, Furbooru and Telegram polled on the server and never on a
desktop install. Nothing errored; they simply never appeared.

These are the first tests to cover the desktop entry point at all. They could
not exist before because importing ``main.py`` pulls pywebview and the tray —
which is exactly why the loop now lives in ``polling/desktop_pollers.py`` and
``main.py`` only calls ``start_all()``.
"""
from __future__ import annotations

import os
import tempfile

import pytest

from polling import desktop_pollers as dp
from polling.multi_account import get_poll_cycles


class _Stop(Exception):
    """Ends the loop from the SLEEP side.

    Not from the poll: ``poller_loop`` catches everything a poll raises, on
    purpose — one platform's failure must never kill its thread. A sentinel
    raised there would be swallowed, which is the point of the design and was
    briefly the point of a broken test.
    """


async def drive(monkeypatch, code="fa", settings=None, last=None, cycle=None,
                max_sleeps=1, **kw):
    """Run ``poller_loop`` on a fake clock until it has slept ``max_sleeps``
    times. Returns ``(polls, slept)`` — what it polled, and what it waited."""
    polls: list[str] = []
    slept: list[float] = []

    async def sleep(secs):
        slept.append(secs)
        if len(slept) >= max_sleeps:
            raise _Stop()

    async def run_cycle(*a, **k):
        if cycle:
            await cycle()

    async def direct(platform, *, run_cycle=None):
        polls.append(platform)
        await run_cycle()

    monkeypatch.setattr(dp, "poll_platform_accounts", direct)
    with pytest.raises(_Stop):
        await dp.poller_loop(code, run_cycle, sleep=sleep,
                             get_settings=lambda: settings if settings is not None else {},
                             last_poll=lambda c: last, **kw)
    return polls, slept


# ── The coverage assertion that did not exist ─────────────────

def test_the_desktop_polls_every_registry_platform():
    """THE bug: sixteen of twenty. Nothing asserted the entry point covered
    the registry, so four platforms were server-only for four releases."""
    assert set(dp.codes()) == set(get_poll_cycles())
    for missing_before in ("e621", "fn", "fbr", "tg"):
        assert missing_before in dp.codes()


def test_one_daemon_thread_per_platform_named_for_it():
    started: list[dict] = []

    class FakeThread:
        def __init__(self, **kw):
            started.append(kw)

        def start(self):
            pass

    codes = dp.start_all(thread_factory=lambda **kw: FakeThread(**kw))
    assert codes == dp.codes()
    assert len(started) == len(get_poll_cycles())
    assert all(t["daemon"] for t in started)
    assert {t["name"] for t in started} == {f"{c}-poller" for c in codes}


def test_the_launch_burst_is_staggered():
    """Twenty first polls at once is a visible stall on a machine also running
    the UI, the tray and pywebview."""
    seen = []

    class FakeThread:
        def __init__(self, **kw):
            seen.append(kw["args"][2])

        def start(self):
            pass

    dp.start_all(thread_factory=lambda **kw: FakeThread(**kw))
    assert seen[0] == 0 and seen[1] == dp.STAGGER_SECONDS
    assert seen == sorted(seen), "stagger must increase monotonically"


# ── Intervals ─────────────────────────────────────────────────

class TestInterval:
    def test_inkbunnys_key_is_the_bare_one(self):
        assert dp.interval_minutes({"poll_interval_minutes": 45}, "ib") == 45
        assert dp.interval_minutes({"tg_poll_interval_minutes": 45}, "tg") == 45

    def test_the_floor_stops_a_mistyped_one_from_hammering_a_site(self):
        assert dp.interval_minutes({"fa_poll_interval_minutes": 1}, "fa") == dp.MIN_INTERVAL_MINUTES
        assert dp.MIN_INTERVAL_MINUTES == 15, "the server's floor"

    def test_absent_or_junk_falls_back_to_the_default(self):
        assert dp.interval_minutes({}, "fa") == 60
        assert dp.interval_minutes({"fa_poll_interval_minutes": "soon"}, "fa") == 60

    @pytest.mark.asyncio
    async def test_the_interval_is_re_read_every_cycle(self, monkeypatch):
        """A change in the UI must take effect on the next cycle, not on restart."""
        settings = {"fa_poll_interval_minutes": 30}

        async def cycle():
            settings["fa_poll_interval_minutes"] = 90

        polls, slept = await drive(monkeypatch, settings=settings, cycle=cycle)
        assert polls == ["fa"] and slept == [90 * 60]


# ── The startup rule (§8 Q1) ──────────────────────────────────

class TestStartup:
    def test_never_polled_is_due_now(self):
        assert dp.startup_delay(3600, None) == 0
        assert dp.startup_delay(3600, "") == 0

    def test_an_unreadable_timestamp_errs_towards_polling(self):
        assert dp.startup_delay(3600, "whenever") == 0

    def test_a_recent_poll_waits_out_the_remainder(self):
        from datetime import datetime, timedelta, timezone
        now = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
        last = (now - timedelta(minutes=10)).isoformat()
        assert dp.startup_delay(3600, last, now=now) == pytest.approx(3000, abs=2)

    def test_an_overdue_poll_fires_immediately(self):
        from datetime import datetime, timedelta, timezone
        now = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
        assert dp.startup_delay(3600, (now - timedelta(hours=5)).isoformat(), now=now) == 0

    def test_nearly_due_counts_as_due(self):
        """Mirrors the server: don't schedule a poll for thirty seconds' time."""
        from datetime import datetime, timedelta, timezone
        now = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
        last = (now - timedelta(minutes=59)).isoformat()
        assert dp.startup_delay(3600, last, now=now) == 0

    def test_a_naive_timestamp_is_read_as_utc(self):
        """The poll logs write datetime('now') — no zone."""
        from datetime import datetime, timedelta, timezone
        now = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
        last = (now - timedelta(minutes=10)).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")
        assert dp.startup_delay(3600, last, now=now) == pytest.approx(3000, abs=2)

    @pytest.mark.asyncio
    async def test_a_launch_with_no_history_polls_before_sleeping(self, monkeypatch):
        """The sixteen threads slept a full hour first — a freshly-opened app
        polled nothing until the user had left it running for an hour."""
        polls, slept = await drive(monkeypatch, last=None)
        assert polls == ["fa"], "the first poll happens before any sleep"
        assert slept == [60 * 60], "and only then does it wait an interval"

    @pytest.mark.asyncio
    async def test_a_recent_poll_is_waited_out_before_the_first_poll(self, monkeypatch):
        from datetime import datetime, timedelta, timezone
        last = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        polls, slept = await drive(monkeypatch, last=last)
        assert polls == [], "nothing is polled until the remainder has elapsed"
        assert slept and slept[0] == pytest.approx(3000, abs=60)


class TestLastFinishedAt:
    @pytest.fixture()
    def db(self, monkeypatch):
        import config as cfg
        from database import db as dbm
        monkeypatch.setattr(cfg, "DB_PATH", os.path.join(tempfile.mkdtemp(), "dp.db"))
        dp._log_table_cache.clear()
        dbm.init_db()
        yield
        dp._log_table_cache.clear()

    def test_every_platform_has_a_poll_log_to_read(self, db):
        """Including the four that never polled on desktop."""
        from database.db import get_connection
        conn = get_connection()
        try:
            for code in get_poll_cycles():
                assert dp._poll_log_table(conn, code), f"{code} has no poll-log table"
            assert dp._poll_log_table(conn, "ib") == "poll_log", "Inkbunny came first"
            assert dp._poll_log_table(conn, "tg") == "tg_poll_log"
        finally:
            conn.close()

    def test_it_reads_the_latest_finished_poll(self, db):
        from database.db import get_connection
        conn = get_connection()
        try:
            conn.execute("INSERT INTO fa_poll_log (started_at, finished_at, status)"
                         " VALUES ('2026-09-01 10:00:00', '2026-09-01 10:01:00', 'success')")
            conn.execute("INSERT INTO fa_poll_log (started_at, finished_at, status)"
                         " VALUES ('2026-09-03 10:00:00', '2026-09-03 10:01:00', 'success')")
            conn.execute("INSERT INTO fa_poll_log (started_at, status) VALUES ('2026-09-04 10:00:00', 'running')")
            conn.commit()
        finally:
            conn.close()
        assert dp.last_finished_at("fa") == "2026-09-03 10:01:00", "a running poll is not a finished one"

    def test_no_rows_reads_as_never_polled(self, db):
        assert dp.last_finished_at("fa") is None


# ── Pause (§3: never read on desktop) ─────────────────────────

class TestPause:
    def test_global_and_per_platform(self):
        assert dp.is_paused({"polling_paused": True}, "fa")
        assert dp.is_paused({"polling_paused_platforms": ["fa"]}, "fa")
        assert not dp.is_paused({"polling_paused_platforms": ["ib"]}, "fa")
        assert not dp.is_paused({}, "fa")

    @pytest.mark.asyncio
    async def test_a_paused_platform_does_not_poll_but_keeps_its_thread(self, monkeypatch):
        """The UI has been writing polling_paused_platforms since the pause
        toggle shipped; only the server ever read it."""
        polls, slept = await drive(monkeypatch, settings={"polling_paused_platforms": ["fa"]},
                                   max_sleeps=3)
        assert polls == [], "paused means not polled"
        assert len(slept) == 3, "the thread keeps looping so an unpause takes effect"

    @pytest.mark.asyncio
    async def test_the_global_pause_still_works(self, monkeypatch):
        polls, _ = await drive(monkeypatch, settings={"polling_paused": True}, max_sleeps=2)
        assert polls == []


class TestFailureIsolation:
    @pytest.mark.asyncio
    async def test_a_raising_cycle_is_logged_and_the_loop_continues(self, monkeypatch):
        async def cycle():
            raise ValueError("platform is down")

        polls, slept = await drive(monkeypatch, cycle=cycle, max_sleeps=3)
        assert len(polls) == 3, "the thread must survive a failing poll and try again"


# ── The copies are gone ───────────────────────────────────────

def _src(path):
    return open(path, encoding="utf-8").read()


def test_main_no_longer_hand_writes_pollers():
    src = _src("main.py")
    for gone in ("_start_poller", "_start_fa_poller", "_start_ig_poller", "_poll_platform_accounts"):
        assert gone not in src, f"main.py still defines {gone}"
    assert "desktop_pollers" in src and "start_all(" in src
    assert "starting %d poller threads" not in src or "11" not in src.split("start_all(")[0][-400:], \
        "the hard-coded thread count is gone"


def test_the_server_reads_the_registry_instead_of_its_own_copy():
    """server.py carried a second twenty-entry map of get_poll_cycles(), with a
    comment in multi_account.py obliging someone to keep them in sync. Two
    copies of one fact is how the desktop drifted."""
    src = _src("server.py")
    assert "get_poll_cycles()" in src
    assert "account_aware = {" not in src, "the hand-written copy is still there"


def test_all_progress_is_derived_from_the_registry():
    """A fourth hand-written list that stopped at e621 — fn, fbr and tg could
    not report progress even on the server."""
    src = _src("routes/api.py")
    assert "get_poll_progress()" in src
    assert '_safe("e621"' not in src, "the hand-written progress list is still there"


def test_the_progress_registry_covers_every_polled_platform():
    from polling.multi_account import get_poll_progress
    assert set(get_poll_progress()) == set(get_poll_cycles())


def test_telegrams_differently_shaped_progress_is_normalised():
    """tg exports {running, platform}; every other poller exports
    {active, phase, current, total, message}. The endpoint normalises rather
    than changing the poller."""
    from routes.api import _normalize_progress
    assert _normalize_progress({"running": True, "platform": "tg"})["active"] is True
    assert _normalize_progress({"active": False, "phase": "idle"})["active"] is False
