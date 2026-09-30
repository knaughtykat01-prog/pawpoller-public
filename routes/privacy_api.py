"""Settings → Privacy: what this install holds, how sensitive it is, and where it can go.

Spec 011 (data classification). Generated entirely from ``datamap`` — the one list that
classifies every table, setting and data file — so a newly classified item shows up here
with no page change. It also lists anything this install holds that the list doesn't know
("unclassified"): that is how an old, long-used install is checked.

Counts and names of kinds only. Never a value: no token, no handle, no person, no file name
inside a data folder.
"""
import logging
import time

from fastapi import APIRouter

import config
import datamap
from database.db import get_connection

logger = logging.getLogger(__name__)

privacy_router = APIRouter(prefix="/api/privacy", tags=["privacy"])

_CACHE_SECONDS = 60
_cache: tuple[float, dict] = (0.0, {})


@privacy_router.get("/holdings")
def holdings():
    """What this install holds, by class and group — counts, never values."""
    global _cache
    now = time.time()
    if _cache[1] and now - _cache[0] < _CACHE_SECONDS:
        return _cache[1]
    conn = get_connection()
    try:
        out = datamap.holdings(conn, config.get_settings(), config.APPDATA_DIR)
    finally:
        conn.close()
    _cache = (now, out)
    return out
