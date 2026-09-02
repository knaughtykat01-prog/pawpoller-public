"""Shared sliding-window rate limiter for X requests.

Deterministic: the limiter takes injectable time/sleep functions, so a fake
clock advanced by the fake sleep lets us assert exact waits with no real delay.
"""

import config
import pytest

from polling.rate_limit import AsyncSlidingWindowLimiter, get_tw_limiter


def _fake_clock():
    clock = [0.0]

    async def sleep(d):
        clock[0] += d

    return clock, sleep


@pytest.mark.asyncio
async def test_admits_up_to_max_without_waiting():
    clock, sleep = _fake_clock()
    lim = AsyncSlidingWindowLimiter(2, 30, time_fn=lambda: clock[0], sleep_fn=sleep)
    await lim.acquire()
    await lim.acquire()
    assert clock[0] == 0.0  # first `max` requests fire instantly


@pytest.mark.asyncio
async def test_waits_when_window_full():
    clock, sleep = _fake_clock()
    lim = AsyncSlidingWindowLimiter(2, 30, time_fn=lambda: clock[0], sleep_fn=sleep)
    await lim.acquire()
    await lim.acquire()          # window full at t=0
    await lim.acquire()          # must wait for the oldest to age out at t=30
    assert clock[0] == 30.0


@pytest.mark.asyncio
async def test_sliding_window_frees_slots_over_time():
    clock, sleep = _fake_clock()
    lim = AsyncSlidingWindowLimiter(2, 30, time_fn=lambda: clock[0], sleep_fn=sleep)
    await lim.acquire()          # t=0
    clock[0] = 20.0
    await lim.acquire()          # t=20 → two in window [0, 20]
    clock[0] = 31.0              # the t=0 stamp is now outside the 30s window
    await lim.acquire()          # admitted with no wait
    assert clock[0] == 31.0


@pytest.mark.asyncio
async def test_zero_window_is_noop():
    lim = AsyncSlidingWindowLimiter(1, 0)
    await lim.acquire()
    await lim.acquire()          # never blocks when the window is disabled


def test_singleton_built_from_config():
    lim = get_tw_limiter()
    assert lim.max_requests == config.TW_RATE_LIMIT_REQUESTS
    assert lim.window_seconds == config.TW_RATE_LIMIT_WINDOW_SECONDS
    assert get_tw_limiter() is lim  # same process-wide instance
