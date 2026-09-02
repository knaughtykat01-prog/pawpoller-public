"""Shared async request rate limiter for X/Twitter polling.

X applies its timeline rate limit **per IP**, shared across ALL of a user's
accounts. Polling several accounts back-to-back in one cycle (server.py
`_poll_accounts` runs them sequentially with no spacing) blew straight through
it — and a per-request `sleep` only paces requests *within* one account.

This module enforces a global cap *across* every account and backend: no more
than ``N`` requests per rolling ``W``-second window. The default (15 / 30 s) is
1 request every 2 s averaged, but as a **sliding window** — a burst can never
exceed 15 in any 30 s, no matter how the accounts interleave.

Scope: the requests PawPoller makes **directly** — the GraphQL scrape
(`clients/tw/client.py`) and the official API (`clients/tw/official_api.py`).
gallery-dl runs as a subprocess and self-paces via ``--sleep-request``, so its
internal requests aren't (and can't cleanly be) gated here.
"""

from __future__ import annotations

import asyncio
import collections
import logging
import time

import config

logger = logging.getLogger(__name__)


class AsyncSlidingWindowLimiter:
    """Admit at most ``max_requests`` acquisitions per ``window_seconds``.

    Admission is FIFO and serialized (the lock is held across the wait), so
    requests fire in order — that ordering *is* the "sequencing". ``time_fn`` /
    ``sleep_fn`` are injectable for deterministic tests.
    """

    def __init__(self, max_requests: int, window_seconds: float, *,
                 time_fn=time.monotonic, sleep_fn=asyncio.sleep):
        self.max_requests = max(1, int(max_requests))
        self.window_seconds = max(0.0, float(window_seconds))
        self._stamps: collections.deque = collections.deque()
        self._lock = asyncio.Lock()
        self._time = time_fn
        self._sleep = sleep_fn

    async def acquire(self) -> None:
        """Block until firing now keeps us within the cap, then record the hit."""
        if self.window_seconds <= 0:
            return
        async with self._lock:
            while True:
                now = self._time()
                cutoff = now - self.window_seconds
                while self._stamps and self._stamps[0] <= cutoff:
                    self._stamps.popleft()
                if len(self._stamps) < self.max_requests:
                    self._stamps.append(now)
                    return
                wait = self._stamps[0] + self.window_seconds - now
                if wait <= 0:
                    # Oldest is exactly at the edge — pop it next loop.
                    self._stamps.popleft()
                    continue
                logger.debug("TW rate limit: waiting %.2fs (%d/%d in %.0fs window)",
                             wait, len(self._stamps), self.max_requests, self.window_seconds)
                await self._sleep(wait)


_tw_limiter: AsyncSlidingWindowLimiter | None = None


def get_tw_limiter() -> AsyncSlidingWindowLimiter:
    """Process-wide singleton limiter for X requests, built from config
    (``TW_RATE_LIMIT_REQUESTS`` / ``TW_RATE_LIMIT_WINDOW_SECONDS``)."""
    global _tw_limiter
    if _tw_limiter is None:
        _tw_limiter = AsyncSlidingWindowLimiter(
            getattr(config, "TW_RATE_LIMIT_REQUESTS", 15),
            getattr(config, "TW_RATE_LIMIT_WINDOW_SECONDS", 30),
        )
    return _tw_limiter


async def tw_acquire() -> None:
    """Await a slot in the shared X request budget before making a request."""
    await get_tw_limiter().acquire()


async def tw_account_stagger(platform: str, polled_count: int,
                             settings: dict | None = None, *,
                             sleep_fn=asyncio.sleep) -> None:
    """Space X account polls so polling *all* accounts never trips the per-IP throttle.

    X's timeline limit is per-IP and shared across accounts: the datacenter IP
    tolerates ~N account-scrapes per window (``TW_ACCOUNT_STAGGER_EVERY``, 2),
    then needs a >8-min reset. So when a cycle polls every X account
    (``tw_roundrobin_batch=0``), we poll in **bursts of N** and sleep
    ``tw_account_stagger_seconds`` (default ``TW_ACCOUNT_STAGGER_SECONDS`` = 480 =
    8 min) **between** bursts — long enough for a fresh window, so each account
    stays on the free gallery-dl path rather than the paid fallback.

    Call this BEFORE polling each X account, passing ``polled_count`` = how many
    X accounts have already been polled this cycle (0 for the first). It sleeps
    only at a burst boundary (``polled_count`` a positive multiple of N), so the
    first burst — and any 1–2 account cycle, or a round-robin batch of 2 — is
    never slowed. No-op for non-X platforms and when the gap is <= 0.
    """
    if platform != "tw":
        return
    if settings is None:
        settings = config.get_settings()
    every = int(getattr(config, "TW_ACCOUNT_STAGGER_EVERY", 2) or 0)
    try:
        gap = float(settings.get("tw_account_stagger_seconds",
                                 getattr(config, "TW_ACCOUNT_STAGGER_SECONDS", 480)))
    except (TypeError, ValueError):
        gap = float(getattr(config, "TW_ACCOUNT_STAGGER_SECONDS", 480))
    if gap > 0 and every > 0 and polled_count > 0 and polled_count % every == 0:
        logger.info("TW: staggering %.0fs before account #%d (per-IP throttle guard)",
                    gap, polled_count + 1)
        await sleep_fn(gap)
