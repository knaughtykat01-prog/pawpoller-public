"""Overnight sheet API (spec 026).

- ``POST /api/overnight/auto`` — on app load: opens the sheet when you've been away for the "Show after" gap
  (``{"show": true, ...summary}``); otherwise just notes the visit. The mark lives on the server, so the
  sheet opens once per absence across every device.
- ``GET  /api/overnight``      — the Overview's Overnight button: the last 24 hours, any time.
- ``POST /api/overnight/seen`` — the sheet was closed (and/or ``show_after`` changed).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException

import config
from database.db import get_connection
from polling import overnight

overnight_router = APIRouter(prefix="/api/overnight", tags=["overnight"])

_NOTE_EVERY = timedelta(minutes=5)     # a page load notes the visit at most this often (no write per load)


def _summary(start: datetime, now: datetime, settings: dict) -> dict:
    from polling.telegram import PLATFORM_NAME
    conn = get_connection()
    try:
        d = overnight.build(conn, start, now, settings.get("display_timezone") or None)
    finally:
        conn.close()
    d["names"] = PLATFORM_NAME
    d["show_after"] = _show_after(settings)
    return d


def _show_after(settings: dict) -> str:
    v = str(settings.get("overnight_show_after") or "6")
    return v if v in overnight.SHOW_AFTER else "6"


@overnight_router.post("/auto")
def auto():
    s = config.get_settings()
    now = datetime.now(timezone.utc)
    last = overnight.parse_ts(s.get("overnight_last_seen_at"))
    if overnight.due(last, _show_after(s), now, s.get("display_timezone") or None):
        d = _summary(last, now, s)
        if d["has_news"]:
            d["show"] = True
            return d
    if last is None or now - last >= _NOTE_EVERY:
        config.save_settings({"overnight_last_seen_at": now.isoformat()})
    return {"show": False}


@overnight_router.get("")
def manual():
    s = config.get_settings()
    now = datetime.now(timezone.utc)
    return _summary(now - timedelta(hours=24), now, s)


@overnight_router.post("/seen")
def seen(payload: dict | None = None):
    payload = payload or {}
    upd = {"overnight_last_seen_at": datetime.now(timezone.utc).isoformat()}
    if "show_after" in payload:
        v = str(payload["show_after"])
        if v not in overnight.SHOW_AFTER:
            raise HTTPException(400, "Unknown choice")
        upd["overnight_show_after"] = v
    config.save_settings(upd)
    return {"ok": True}
