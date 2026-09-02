"""Shared follower-capture hook for the platform pollers.

Every poller reuses its already-authenticated client to fetch the tracked
account's follower count once per cycle and append it to the shared
``account_follower_snapshots`` series (see ``database/followers.py``).

Two invariants this helper enforces so callers don't have to:

- **Never hold a SQLite write lock across a network await** (poller gotcha #10):
  the follower count is fetched *before* any DB write, then the snapshot is
  written and committed with no await in between.
- **Never let follower capture break a poll cycle**: any error (missing method,
  network failure, unexpected response) is swallowed at debug level. Follower
  data is a nice-to-have layered on top of the core snapshot; it must not fail
  the poll that already succeeded.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


async def capture_followers(client, account_id, conn) -> bool:
    """Fetch ``client``'s follower count and record a snapshot for ``account_id``.

    Returns True if a snapshot was written. No-op (returns False) when the client
    has no ``get_follower_count`` method, the fetch fails, or the platform can't
    report a count. Commits its own write on success.
    """
    if account_id is None or conn is None:
        return False
    try:
        getter = getattr(client, "get_follower_count", None)
        if getter is None:
            return False
        count = await getter()          # network — done BEFORE touching the DB
        if count is None:
            return False
        from database import followers as followers_db
        wrote = followers_db.record_snapshot(conn, account_id, count)
        conn.commit()
        if wrote:
            logger.info("Recorded follower count %s for account %s", count, account_id)
        return wrote
    except Exception:
        logger.debug("Follower capture failed for account %s", account_id, exc_info=True)
        return False
