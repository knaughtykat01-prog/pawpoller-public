"""Spec 037: seats poll at their own clock slots and take turns at each site."""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import textwrap
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from polling import schedule, site_turns

H = 3600


def _at(y, mo, d, h, mi, s=0) -> float:
    return datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc).timestamp()


# ── US1: slots ───────────────────────────────────────────────────────────────

def test_a_slot_is_the_start_minute_on_the_interval():
    nxt = schedule.next_slot(_at(2026, 10, 13, 9, 30), 12, 4 * H)
    assert datetime.fromtimestamp(nxt, timezone.utc) == datetime(2026, 10, 13, 12, 12, tzinfo=timezone.utc)


def test_an_overrun_rolls_to_the_next_slot_never_a_catch_up():
    # The 8:12 poll ran until 12:40: the 12:12 slot is gone, so the next is 16:12, not "now".
    nxt = schedule.next_slot(_at(2026, 10, 13, 12, 40), 12, 4 * H)
    assert datetime.fromtimestamp(nxt, timezone.utc).hour == 16


def test_exactly_on_a_slot_means_the_next_one():
    t = _at(2026, 10, 13, 12, 12)
    assert schedule.next_slot(t, 12, 4 * H) == t + 4 * H


def test_slots_never_drift_over_a_week_of_uneven_polls():
    t, starts = _at(2026, 10, 13, 0, 0), []
    durations = [60, 2400, 30, 900, 1800, 120]          # 1 to 40 minutes
    for i in range(42):                                 # a week of four-hourly polls
        t = schedule.next_slot(t, 12, 4 * H)
        starts.append(t)
        t += durations[i % len(durations)]
    assert all(datetime.fromtimestamp(s, timezone.utc).minute == 12 for s in starts)
    assert all(b - a == 4 * H for a, b in zip(starts, starts[1:]))


def test_slots_are_utc_so_daylight_saving_never_moves_them():
    a = schedule.next_slot(_at(2026, 10, 3, 23, 0), 6, H)
    b = schedule.next_slot(_at(2026, 10, 4, 23, 0), 6, H)     # Sydney changed its clocks between these
    assert datetime.fromtimestamp(a, timezone.utc).minute == datetime.fromtimestamp(b, timezone.utc).minute == 6


def test_offset_zero_is_a_seat_and_bad_values_are_not(monkeypatch):
    monkeypatch.setenv("PAWPOLLER_POLL_OFFSET_MINUTES", "0")
    assert schedule.slot_mode() and schedule.offset_minutes() == 0
    for bad in ("", "abc", "60", "-1"):
        monkeypatch.setenv("PAWPOLLER_POLL_OFFSET_MINUTES", bad)
        assert not schedule.slot_mode()
    monkeypatch.delenv("PAWPOLLER_POLL_OFFSET_MINUTES")
    assert not schedule.slot_mode()


def test_seats_spread_six_minutes_apart_stay_apart():
    t = _at(2026, 10, 13, 0, 1)
    minutes = {datetime.fromtimestamp(schedule.next_slot(t, off, H), timezone.utc).minute for off in range(0, 60, 6)}
    assert len(minutes) == 10


# ── US2: one seat per site ───────────────────────────────────────────────────

def test_every_poll_cycle_takes_its_sites_turn():
    """A new site can't forget the turn: the registry is checked, code by code."""
    from polling.multi_account import get_poll_cycles
    cycles = get_poll_cycles()
    assert len(cycles) >= 26
    assert {code: getattr(fn, "__site_turn__", None) for code, fn in cycles.items()} == {c: c for c in cycles}


def test_the_telegram_poll_command_uses_the_same_turned_cycles():
    src = (Path(__file__).resolve().parents[1] / "polling" / "telegram_bot.py").read_text(encoding="utf-8")
    assert '"ib": run_poll_cycle' in src     # the same imported (decorated) functions, not copies


def test_off_the_box_the_turn_is_a_pass_through(monkeypatch):
    monkeypatch.delenv("PAWPOLLER_TURN_DIR", raising=False)
    calls = []

    @site_turn_for("fa")
    async def cycle(x, force_full=False):
        calls.append((x, force_full))
        return {"ok": 1}

    assert asyncio.run(cycle(3, force_full=True)) == {"ok": 1} and calls == [(3, True)]


def site_turn_for(code):
    return site_turns.site_turn(code)


def test_an_unusable_turn_folder_is_skipped_not_fatal(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("PAWPOLLER_TURN_DIR", str(tmp_path / "missing"))
    monkeypatch.setattr(site_turns, "_warned", False)

    @site_turns.site_turn("fa")
    async def cycle():
        return "ran"

    assert asyncio.run(cycle()) == "ran"
    assert site_turns.enabled() is None


_needs_flock = pytest.mark.skipif(site_turns.fcntl is None, reason="flock is Linux/macOS only (the box is Linux)")

_HOLDER = textwrap.dedent("""
    import fcntl, sys, time
    f = open(sys.argv[1], "a+")
    fcntl.flock(f.fileno(), fcntl.LOCK_EX)
    print("held", flush=True)
    time.sleep(float(sys.argv[2]))
""")


def _hold(lock: Path, seconds: float) -> subprocess.Popen:
    p = subprocess.Popen([sys.executable, "-c", _HOLDER, str(lock), str(seconds)], stdout=subprocess.PIPE, text=True)
    assert p.stdout.readline().strip() == "held"
    return p


@_needs_flock
def test_a_second_seat_waits_for_the_turn_then_runs(monkeypatch, tmp_path):
    monkeypatch.setenv("PAWPOLLER_TURN_DIR", str(tmp_path))
    monkeypatch.setattr(site_turns, "RETRY_SECONDS", 0.1)
    other = _hold(tmp_path / "fa.lock", 1.0)          # another seat is checking FurAffinity
    started = time.monotonic()

    @site_turns.site_turn("fa")
    async def cycle():
        return time.monotonic()

    ran_at = asyncio.run(cycle())
    other.wait()
    assert ran_at - started >= 0.8                     # it waited for the other seat to finish


@_needs_flock
def test_different_sites_dont_wait_for_each_other(monkeypatch, tmp_path):
    monkeypatch.setenv("PAWPOLLER_TURN_DIR", str(tmp_path))
    other = _hold(tmp_path / "fa.lock", 2.0)
    try:
        @site_turns.site_turn("e621")
        async def cycle():
            return "ran"
        t = time.monotonic()
        assert asyncio.run(cycle()) == "ran" and time.monotonic() - t < 0.5
    finally:
        other.kill()
        other.wait()


@_needs_flock
def test_a_dead_seat_frees_its_turn_at_once(monkeypatch, tmp_path):
    monkeypatch.setenv("PAWPOLLER_TURN_DIR", str(tmp_path))
    monkeypatch.setattr(site_turns, "RETRY_SECONDS", 0.1)
    other = _hold(tmp_path / "fa.lock", 60)
    other.kill()                                       # crashed mid-check
    other.wait()

    @site_turns.site_turn("fa")
    async def cycle():
        return "ran"

    t = time.monotonic()
    assert asyncio.run(cycle()) == "ran" and time.monotonic() - t < 1


@_needs_flock
def test_a_stuck_turn_is_given_up_after_the_limit(monkeypatch, tmp_path):
    monkeypatch.setenv("PAWPOLLER_TURN_DIR", str(tmp_path))
    monkeypatch.setenv("PAWPOLLER_TURN_WAIT_SECONDS", "0.5")
    monkeypatch.setattr(site_turns, "RETRY_SECONDS", 0.1)
    other = _hold(tmp_path / "fa.lock", 5)
    ran = []
    try:
        @site_turns.site_turn("fa")
        async def cycle():
            ran.append(1)

        with pytest.raises(site_turns.TurnTimeout) as e:
            asyncio.run(cycle())
        assert ran == [] and "another PawPoller on this server" in str(e.value)
    finally:
        other.kill()
        other.wait()


@_needs_flock
def test_the_turn_is_released_when_the_cycle_fails(monkeypatch, tmp_path):
    monkeypatch.setenv("PAWPOLLER_TURN_DIR", str(tmp_path))

    @site_turns.site_turn("fa")
    async def boom():
        raise RuntimeError("site said no")

    with pytest.raises(RuntimeError):
        asyncio.run(boom())
    import fcntl
    with open(tmp_path / "fa.lock", "a+") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)     # free again


@_needs_flock
def test_waiting_shows_in_the_sites_progress(monkeypatch, tmp_path):
    monkeypatch.setenv("PAWPOLLER_TURN_DIR", str(tmp_path))
    monkeypatch.setattr(site_turns, "RETRY_SECONDS", 0.1)
    from polling.multi_account import get_poll_progress
    prog = get_poll_progress()["fa"]
    seen = []
    other = _hold(tmp_path / "fa.lock", 0.6)

    async def watch():
        for _ in range(10):
            seen.append(prog.get("phase"))
            await asyncio.sleep(0.05)

    @site_turns.site_turn("fa")
    async def cycle():
        return "ran"

    async def both():
        return await asyncio.gather(cycle(), watch())

    asyncio.run(both())
    other.wait()
    assert "waiting" in seen


@_needs_flock
def test_a_link_planted_in_the_turn_folder_is_never_followed(monkeypatch, tmp_path):
    """Security review 4.72.0: a compromised seat swaps fa.lock for a link to the database; others must not touch it."""
    turns, victim = tmp_path / "turns", tmp_path / "pawpoller.db"
    turns.mkdir()
    victim.write_bytes(b"precious library")
    (turns / "fa.lock").symlink_to(victim)
    monkeypatch.setenv("PAWPOLLER_TURN_DIR", str(turns))
    ran = []

    @site_turns.site_turn("fa")
    async def cycle():
        ran.append(1)

    assert asyncio.run(cycle()) is None and ran == [1]              # the link was replaced, the poll ran
    assert victim.read_bytes() == b"precious library"               # and the file it pointed at is untouched
    assert not (turns / "fa.lock").is_symlink()


@_needs_flock
def test_a_squatted_turn_name_is_cleared_so_the_site_isnt_blocked(monkeypatch, tmp_path):
    """Release review 4.72.0: a folder or pipe named fa.lock must not stop every seat polling FurAffinity."""
    monkeypatch.setenv("PAWPOLLER_TURN_DIR", str(tmp_path))
    (tmp_path / "fa.lock").mkdir()
    os.mkfifo(tmp_path / "e621.lock")

    @site_turns.site_turn("fa")
    async def fa():
        return "fa ran"

    @site_turns.site_turn("e621")
    async def e621():
        return "e621 ran"

    assert asyncio.run(fa()) == "fa ran" and asyncio.run(e621()) == "e621 ran"


@_needs_flock
def test_a_second_poll_of_a_site_from_the_same_seat_doesnt_wait_on_itself(monkeypatch, tmp_path):
    """Review 4.72.0: flock is per open file, so Poll Now during the scheduled poll would wait on its own seat.
    It must pass straight through, so the poller's own lock skips it as it always has."""
    monkeypatch.setenv("PAWPOLLER_TURN_DIR", str(tmp_path))
    monkeypatch.setenv("PAWPOLLER_TURN_WAIT_SECONDS", "1")
    gate = None

    @site_turns.site_turn("fa")
    async def cycle(name):
        if name == "scheduled":
            await gate.wait()
        return name

    async def both():
        nonlocal gate
        gate = asyncio.Event()
        first = asyncio.create_task(cycle("scheduled"))
        await asyncio.sleep(0.05)
        t = time.monotonic()
        second = await cycle("poll now")
        quick = time.monotonic() - t < 0.5
        gate.set()
        return await first, second, quick

    assert asyncio.run(both()) == ("scheduled", "poll now", True)


@_needs_flock
def test_waiting_is_cleared_once_the_turn_is_taken(monkeypatch, tmp_path):
    """Telegram's poller never writes its own progress, so a past wait must not stay on screen."""
    monkeypatch.setenv("PAWPOLLER_TURN_DIR", str(tmp_path))
    monkeypatch.setattr(site_turns, "RETRY_SECONDS", 0.1)
    from polling.multi_account import get_poll_progress
    prog = get_poll_progress()["tg"]
    other = _hold(tmp_path / "tg.lock", 0.4)

    @site_turns.site_turn("tg")
    async def cycle():
        return prog.get("phase")

    phase_inside = asyncio.run(cycle())
    other.wait()
    assert phase_inside != "waiting" and prog.get("phase") != "waiting"


# ── US3 / US4 ────────────────────────────────────────────────────────────────

def test_robots_txt_says_list_nothing_signed_out():
    from fastapi.testclient import TestClient
    import dashboard
    r = TestClient(dashboard.app).get("/robots.txt")
    assert r.status_code == 200 and r.text == "User-agent: *\nDisallow: /\n"
    assert "/robots.txt" in dashboard._AUTH_EXEMPT_PATHS


def test_a_move_leaves_the_relay_behind():
    from routes import move_api
    assert move_api.keeps_here("cf_worker_url") and move_api.keeps_here("cf_worker_key")


def test_the_orchestrator_waits_for_slots_on_a_seat():
    """Wiring guard: the seat path waits for its slot instead of sleeping an interval after the poll."""
    src = (Path(__file__).resolve().parents[1] / "server.py").read_text(encoding="utf-8")
    assert "_schedule.slot_mode()" in src and "await _wait_for_slot()" in src
    assert src.count("await _wait_for_slot()") == 2     # at start-up and after every cycle
