"""Keep each poller's cached client on the event loop that built it.

Every ``polling/<x>_poller.py`` caches one client per process
(``_get_or_create_client``), but PawPoller runs several event loops, one per
thread: the dashboard (uvicorn), the server's poll orchestrator, the desktop's
per-platform poller threads (``polling/desktop_pollers.py``) and the Telegram
bot. The client's ``httpx.AsyncClient`` keeps pooled keep-alive connections,
and each one holds an ``asyncio.Event`` bound to the loop that opened it. Reuse
that connection from another loop inside httpx's keep-alive window and the
request dies with

    <asyncio.locks.Event object at 0x… [unset]> is bound to a different event loop

— seen on a desktop install as Instagram's session dot going amber after
"Check sessions now" (dashboard loop) landed while the Instagram poller thread
was mid-cycle on the same client. Any manual action (Check now, Poll now, a
connect route, the bot's /poll) racing a scheduled poll could hit it, on any
of the nineteen cached platforms.

So a getter asks :func:`reusable` before handing its cached client out and
builds a fresh one when the answer is no, then returns it through :func:`pin`.
"""
from __future__ import annotations

import asyncio


def _running_loop():
    try:
        return asyncio.get_running_loop()
    except RuntimeError:   # called from sync code (tests, startup)
        return None


def reusable(client) -> bool:
    """True when *client* exists and was handed out on the loop running now."""
    return client is not None and getattr(client, "_home_loop", None) is _running_loop()


def pin(client):
    """Stamp the running loop on *client* and return it.

    Only reached after :func:`reusable` said yes or the client was just built,
    so it never re-homes a client that holds another loop's connections.
    """
    # ponytail: the client this replaces is dropped unclosed (it belongs to
    # another loop, so it can't be closed from here); GC reclaims its sockets.
    # Per-loop caches if manual actions ever get frequent enough to churn.
    client._home_loop = _running_loop()
    return client
