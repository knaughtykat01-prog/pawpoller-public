"""Shared follower-count API (/api/followers/*).

One endpoint serves the follower stat card + growth chart for every platform
whose poller records a follower count (see ``database/followers.py`` and
``polling/followers.py``). Follower history lives in a single cross-platform
table keyed by the global account_id, so — unlike the per-platform submission
analytics — a single router covers all of them.

The per-account current count is also carried on each row of ``/api/accounts``
(the accounts table caches ``follower_count``), so the Accounts page needs no
call here; this router is for the platform dashboards' card + chart.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query

from database.db import get_connection
from database import followers as followers_db

logger = logging.getLogger(__name__)

followers_router = APIRouter(prefix="/api/followers")


@followers_router.get("/{platform}")
def get_followers(
    platform: str,
    account_id: int | None = Query(None, description="Scope to one account; default = platform default"),
    since: str | None = Query(None, description="ISO date lower bound for the series"),
):
    """Current follower count + growth series for a platform (or one account).

    Returns ``{followers, at, series, supported}``. Platforms with no follower
    source return ``supported=false`` and empty data rather than an error, so the
    frontend can call this uniformly for any platform.
    """
    if platform not in followers_db.FOLLOWER_PLATFORMS:
        return {"platform": platform, "supported": False,
                "followers": None, "at": None, "series": []}
    conn = get_connection()
    try:
        latest = followers_db.platform_latest(conn, platform, account_id=account_id) or {}
        series = followers_db.platform_series(conn, platform, account_id=account_id, since=since)
        return {
            "platform": platform,
            "supported": True,
            "followers": latest.get("followers"),
            "at": latest.get("at"),
            "series": series,
        }
    except Exception as e:
        logger.error("Error in /api/followers/%s: %s", platform, e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()
