"""Trello sync API — configuration, preview, sync, conflicts (spec 005).

Contract: `specs/005-trello-commissions/contracts/api.md`.

⚠ **No route here may echo the API key or the token**, in a body, an error string
or a log line. Trello repeats query parameters in some error bodies, so the
upstream text is never passed through — `clients/trello/client.py` redacts before
raising and this module reports the exception's message, not the response.

⚠ **The sync report carries client names and prices.** It is an authenticated API
response and a screen; it is never logged and never written to a file.
"""
from __future__ import annotations

import logging
import threading

from fastapi import APIRouter, HTTPException

import config
from clients.trello.client import TrelloAuthError, TrelloClient, TrelloError
from database import commissions_queries as cq
from database import trello_queries as tq
from database.db import get_connection
from trello import diff, mapping, sync

logger = logging.getLogger(__name__)

trello_router = APIRouter(prefix="/api/trello", tags=["trello"])

# One sync at a time. Two concurrent runs would each read the board, then each
# write from a snapshot the other had already invalidated.
_sync_lock = threading.Lock()


def _client_or_400(body: dict | None = None) -> TrelloClient:
    if body and (body.get("key") or body.get("token")):
        key, token = str(body.get("key") or ""), str(body.get("token") or "")
    else:
        key, token = sync.credentials()
    try:
        return TrelloClient(key, token)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ── Configuration ────────────────────────────────────────────────────────────

@trello_router.post("/test")
def test_credentials(body: dict):
    """Whose credentials these are. Saves nothing and writes nothing to Trello."""
    client = _client_or_400(body)
    try:
        me = client.me()
    except TrelloAuthError as e:
        return {"ok": False, "error": str(e)}
    except TrelloError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "member": me}


@trello_router.get("/boards")
def list_boards():
    client = _client_or_400()
    try:
        return {"boards": client.boards()}
    except TrelloError as e:
        raise HTTPException(status_code=502, detail=str(e))


@trello_router.get("/boards/{board_id}/lists")
def list_board_lists(board_id: str):
    client = _client_or_400()
    try:
        return {"lists": client.lists(board_id)}
    except TrelloError as e:
        raise HTTPException(status_code=502, detail=str(e))


@trello_router.get("/config")
def get_config():
    cfg = mapping.get_config()
    is_owner, owner = mapping.owner_state()
    key, token = sync.credentials()
    return {**cfg, "is_owner": is_owner, "owner_tag": owner,
            "has_credentials": bool(key and token),
            "instance_tag": mapping.instance_tag(),
            "statuses": list(cq.STATUSES),
            "unmapped": mapping.unmapped_statuses()}


@trello_router.put("/config")
def put_config(body: dict):
    fields = {}
    if "board_id" in body:
        # Picking a board claims it for this instance. Changing boards re-claims,
        # and resets the first-sync preview gate — a different board has never
        # been previewed, whatever the old one had.
        return {**mapping.claim(str(body.get("board_id") or ""),
                                str(body.get("board_name") or "")), "claimed": True}
    for k in ("list_map", "interval_min"):
        if k in body:
            fields[k] = body[k]
    if not fields:
        raise HTTPException(status_code=400, detail="Nothing to change.")
    return mapping.save_config(**fields)


# ── Sync ─────────────────────────────────────────────────────────────────────

def _run(apply: bool, confirm_unlinks: bool = False) -> dict:
    if not _sync_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="A Trello sync is already running.")
    try:
        conn = get_connection()
        try:
            return sync.run_sync(conn, apply=apply, confirm_unlinks=confirm_unlinks)
        finally:
            conn.close()
    finally:
        _sync_lock.release()


@trello_router.post("/preview")
def preview():
    """Everything the next sync would do. Writes nothing, ever."""
    return _run(apply=False)


@trello_router.post("/sync")
def do_sync(body: dict | None = None):
    """Apply. ⚠ Without ``confirm: true`` this previews instead.

    The first sync against a board previews regardless — `trello/sync.py` enforces
    that, because a route flag is the wrong place for a rule about the board.
    """
    body = body or {}
    return _run(apply=bool(body.get("confirm")),
                confirm_unlinks=bool(body.get("confirm_unlinks")))


@trello_router.get("/status")
def status():
    conn = get_connection()
    try:
        cfg = mapping.get_config()
        links = tq.list_links(conn, cfg["board_id"])
        last = max((l.get("last_synced_at") or "") for l in links) if links else ""
        return {
            "configured": mapping.is_configured(),
            "is_owner": mapping.owner_state()[0],
            "linked": len(links),
            "last_sync_at": last,
            "open_conflicts": tq.count_conflicts(conn),
            "running": _sync_lock.locked(),
        }
    finally:
        conn.close()


# ── Conflicts ────────────────────────────────────────────────────────────────

@trello_router.get("/conflicts")
def list_conflicts():
    conn = get_connection()
    try:
        return {"conflicts": tq.list_conflicts(conn)}
    finally:
        conn.close()


@trello_router.post("/conflicts/resolve")
def resolve_conflict(body: dict):
    """Choose a side for ONE field.

    ⚠ There is deliberately no "resolve all". A bulk button on a screen whose
    whole purpose is "look at these two values" defeats the screen.
    """
    client_name = str(body.get("client_name") or "")
    created_at = body.get("created_at")
    field = str(body.get("field") or "")
    side = str(body.get("side") or "").lower()
    if side not in ("local", "remote") or field not in diff.FIELDS:
        raise HTTPException(status_code=400,
                            detail="Choose 'local' or 'remote' for a known field.")

    conn = get_connection()
    try:
        rows = [c for c in tq.list_conflicts(conn, client_name, created_at)
                if c["field"] == field]
        if not rows:
            raise HTTPException(status_code=404, detail="No such conflict.")
        row = rows[0]

        if side == "remote":
            # Write Trello's value into the commission, then agree on it.
            commission = _find_commission(conn, client_name, created_at)
            if not commission:
                raise HTTPException(status_code=404, detail="No such commission.")
            value = row["remote_value"]
            if field == "archived":
                cq.update_commission(conn, commission["id"],
                                     archived=1 if value in ("1", "True", "true") else 0)
            elif field in ("status", "description", "currency", "due_date", "price"):
                cq.update_commission(conn, commission["id"], **{field: value})
            conn.commit()
            tq.set_baseline_field(conn, client_name, created_at, field, value)
        else:
            # Keep ours. The next sync sees local == baseline is false on the
            # REMOTE side only, so it pushes — no write to Trello from here.
            tq.set_baseline_field(conn, client_name, created_at, field,
                                  row["remote_value"])

        tq.clear_conflict(conn, client_name, created_at, field)
        return {"resolved": True, "field": field, "side": side}
    finally:
        conn.close()


def _find_commission(conn, client_name: str, created_at: str) -> dict | None:
    for archived in (False, True):
        for c in cq.list_commissions(conn, archived=archived):
            if c.get("client_name") == client_name and c.get("created_at") == created_at:
                return c
    return None


# ── Candidates and unlink ────────────────────────────────────────────────────

@trello_router.get("/candidates")
def candidates():
    """Cards in mapped columns that no commission claims. Nothing is created."""
    return {"candidates": _run(apply=False).get("candidates", [])}


@trello_router.post("/candidates/import")
def import_candidate(body: dict):
    """Turn one card into a commission, deliberately — never automatically."""
    card_id = str(body.get("card_id") or "")
    if not card_id:
        raise HTTPException(status_code=400, detail="Which card?")
    client = _client_or_400()
    cfg = mapping.get_config()
    try:
        card = client.card(card_id)
    except TrelloError as e:
        raise HTTPException(status_code=502, detail=str(e))
    if not card:
        raise HTTPException(status_code=404, detail="No such card.")

    status_name = mapping.status_for_list(card["id_list"]) or "quote"
    conn = get_connection()
    try:
        if tq.get_link_by_card(conn, card_id):
            raise HTTPException(status_code=409, detail="That card is already linked.")
        # The card's title becomes the client; there is nothing else to split on,
        # and guessing at a separator would silently mangle a real name.
        cid = cq.create_commission(
            conn, client_name=card["name"][:200] or "Imported card",
            description="", status=status_name,
            due_date=(card["due"] or "")[:10])
        conn.commit()
        commission = cq.get_commission(conn, cid)
        local = diff.normalise_local(commission)
        remote = diff.normalise_remote(card, status_name)
        tq.create_link(conn, client_name=commission["client_name"],
                       created_at=commission["created_at"], card_id=card_id,
                       board_id=cfg["board_id"], card_url=card["url"],
                       baseline=dict(remote) or dict(local))
        return commission
    finally:
        conn.close()


@trello_router.post("/unlink")
def unlink(body: dict):
    """Forget the card. Writes nothing to Trello and keeps the commission."""
    conn = get_connection()
    try:
        tq.delete_link(conn, str(body.get("client_name") or ""), body.get("created_at"))
        return {"unlinked": True}
    finally:
        conn.close()
