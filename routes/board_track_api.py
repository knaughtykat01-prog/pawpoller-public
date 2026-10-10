"""Track settings for image-board accounts (spec 028): /api/board-track/{account_id}.

What an e621 or Furbooru account follows besides its own uploads (its artist tag, its characters) and
whether posts other people uploaded count in its totals. The searching itself happens in the pollers
(``polling/board_track.py``); this only reads and saves the choices and helps fill in the artist tag.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

import config
from database.db import get_connection
from polling import board_track

logger = logging.getLogger(__name__)
board_track_router = APIRouter(prefix="/api/board-track")


def _account(conn, account_id: int) -> dict:
    from database import accounts as accounts_db
    a = accounts_db.get_account(conn, account_id)
    if not a or a.get("platform") not in board_track.BOARDS:
        raise HTTPException(404, "Not an e621, Furbooru or Rule34 account")
    return a


def _client(a: dict):
    creds = config.resolve_account_credentials(a["platform"], a["account_id"], bool(a.get("is_default")),
                                               config.get_settings())
    p = a["platform"]
    if p == "e621":
        from clients.e621.client import E621Client
        return E621Client(creds.get("e621_username", ""), creds.get("e621_api_key", ""))
    if p == "r34":
        from clients.r34.client import Rule34Client
        return Rule34Client(creds.get("r34_username", ""), creds.get("r34_api_key", ""), creds.get("r34_user_id", ""))
    from clients.fbr.client import FurbooruClient
    return FurbooruClient(username=creds.get("fbr_username", ""), api_key=creds.get("fbr_api_key", ""))


@board_track_router.get("/{account_id}")
def get_track(account_id: int):
    conn = get_connection()
    try:
        a = _account(conn, account_id)
        tagged, untagged = board_track.my_characters(conn, account_id)
        found = board_track.found_summary(conn, a["platform"], account_id)
    finally:
        conn.close()
    t = board_track.get(account_id)
    return {"account_id": account_id, "platform": a["platform"], "saved": t["saved"],
            "settings": {k: t[k] for k in board_track.DEFAULTS},
            "characters": tagged, "characters_untagged": untagged, "found": found}


@board_track_router.put("/{account_id}")
def put_track(account_id: int, body: dict):
    from datetime import datetime, timezone
    conn = get_connection()
    try:
        a = _account(conn, account_id)
        code = a["platform"]
        try:
            tags = board_track.validate_tags(code, body.get("artist_tags") or [])
        except ValueError as e:
            raise HTTPException(400, str(e))
        before = board_track.get(account_id)
        count_others = bool(body.get("count_others", True))
        changes = {"artist_tags": tags, "characters": bool(body.get("characters")),
                   "count_others": count_others, "saved_at": datetime.now(timezone.utc).isoformat()}
        if "artist_verified" in body:
            changes["artist_verified"] = bool(body["artist_verified"]) and code == "e621"
        moved = 0
        if count_others != before["count_others"]:
            moved = board_track.move(conn, code, account_id, to_found=not count_others)
            conn.commit()
        board_track.update(account_id, **changes)
    finally:
        conn.close()
    return {"ok": True, "moved": moved}


@board_track_router.get("/{account_id}/suggest")
async def suggest(account_id: int):
    """The artist tag: e621's verified link to this account, else the People page's handle."""
    conn = get_connection()
    try:
        a = _account(conn, account_id)
        persona = a.get("persona_id")
        sql = ("SELECT h.handle FROM artist_handles h JOIN artists ar ON ar.artist_key = h.artist_key"
               " WHERE h.platform = ? AND ar.persona_id " + ("= ?" if persona else "IS NOT NULL") + " LIMIT 1")
        try:
            row = conn.execute(sql, (a["platform"], persona) if persona else (a["platform"],)).fetchone()
        except Exception:  # noqa: BLE001 — an older People table: no suggestion from it
            row = None
    finally:
        conn.close()
    if a["platform"] == "e621":
        try:
            async with _client(a) as c:
                uid = board_track.get(account_id).get("user_id") or await c.get_user_id()
                tag = await c.linked_artist(uid)
            if tag:
                return {"tag": tag, "source": "e621", "verified": True}
        except Exception as e:  # noqa: BLE001
            logger.info("e621 artist link lookup failed: %s", e)
    if row and row[0]:
        return {"tag": board_track.norm_tag(a["platform"], row[0]), "source": "people", "verified": False}
    return {"tag": None, "source": None, "verified": False}


@board_track_router.get("/{account_id}/tag-count")
async def tag_count(account_id: int, tag: str):
    conn = get_connection()
    try:
        a = _account(conn, account_id)
    finally:
        conn.close()
    try:
        t = board_track.validate_tags(a["platform"], [tag])[0]
    except (ValueError, IndexError) as e:
        raise HTTPException(400, str(e) or "Empty tag")
    async with _client(a) as c:
        return {"tag": t, "count": await c.tag_count(t)}
