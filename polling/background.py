"""Fire-and-forget scheduling for manual poll / resync triggers.

Manual trigger endpoints used to ``await run_X_poll_cycle()`` *inside* the HTTP
request, so the response didn't return until the whole scrape finished. Behind
Cloudflare (which caps a request at ~100 s) a slow platform — AO3, X — blew
past that and the browser got a **524 timeout**, even though the poll itself was
running fine.

``spawn()`` runs the cycle as a detached background task on the running event
loop and lets the endpoint return immediately. The frontend only cares that the
trigger was accepted (it shows "Done!" then reloads), not about the poll's
return value, so returning before the scrape completes is exactly right.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable

logger = logging.getLogger(__name__)

# Hold strong references to in-flight tasks. asyncio only keeps a weak reference
# to a bare create_task() result, so without this the task can be garbage
# collected mid-flight and cancelled. Discarded in the done-callback.
_background_tasks: set[asyncio.Task] = set()


def spawn(coro: Awaitable, label: str) -> None:
    """Schedule ``coro`` fire-and-forget, logging any exception it raises.

    Must be called from within a running event loop (i.e. an async route
    handler), which is always the case for the poll/resync endpoints.
    """
    task = asyncio.ensure_future(coro)
    _background_tasks.add(task)

    def _done(t: asyncio.Task) -> None:
        _background_tasks.discard(t)
        if t.cancelled():
            return
        exc = t.exception()
        if exc is not None:
            logger.error("Background task %r failed: %s", label, exc, exc_info=exc)

    task.add_done_callback(_done)


def spawn_poll(coro: Awaitable, label: str) -> None:
    """``spawn()``, but only if this instance owns polling. Otherwise 409.

    ``config.get_polling_owner()`` gates the background poll LOOP, and that was
    the whole of the enforcement: every manual trigger — /api/poll/trigger,
    /poll/full-resync and the per-platform pair on all 19 platform routers —
    ran the identical cycle with no ownership check at all. So a paired desktop
    clicking "Poll now" wrote the analytics tables the server owns, while the
    server was polling them too: duplicate requests against every platform's
    rate limit, duplicate snapshot rows, and two instances racing on the same
    submission rows.

    Enforced here rather than at ~39 call sites because every manual trigger
    already funnels through this module, so there is no way to add a poll route
    later and forget the guard. ``spawn()`` itself stays ungated — it also
    carries the manual session-health check, which is read-only and legitimate
    from either side.

    Ownership is a property of THIS PROCESS, so it is resolved per call rather
    than cached: pairing can be turned on or off in Settings without a restart.
    """
    # Deferred: posting.scheduler pulls in the manager, and polling.background is
    # imported by every platform router. Same pattern as routes/artwork_api.py.
    from fastapi import HTTPException
    import config
    from posting.scheduler import detect_runtime_mode

    runtime = detect_runtime_mode()
    if config.get_polling_owner(runtime) != "local":
        # The caller built the coroutine before handing it over, so refusing
        # without closing it emits "coroutine ... was never awaited" — once per
        # click, in a log that gets read during incidents.
        close = getattr(coro, "close", None)
        if callable(close):
            close()
        logger.info("Refused manual poll %r — this instance is not the polling owner "
                    "(runtime=%s)", label, runtime)
        raise HTTPException(
            409,
            detail="This instance is paired to a server, which owns polling. "
                   "Run the poll from the server's dashboard instead.",
        )
    spawn(coro, label)
