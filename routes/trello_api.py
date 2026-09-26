"""Trello mirror API (spec 006). Contract: `specs/006-trello-board-mirror/contracts/api.md`.

Every READ is served from the local mirror. Every WRITE updates the mirror and
queues an outbox op in one transaction, wakes the worker, and returns at once —
a route never waits on Trello (research R4).

⚠ **No route here may echo the API key, the token or the secret**, in a body, an
error string or a log line.

⚠ **Board content is personal data** — card text, comments, member names, client
names and prices. It goes to the operator's screen in an authenticated response
and nowhere else: never logged, never in an error message.
"""
from __future__ import annotations

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse

import config
from clients.trello.client import COVER_COLOURS, TrelloClient, TrelloError
from database import commissions_queries as cq
from database import trello_queries as tq
from database.db import get_connection
from trello import commission, covers, mapping, mirror, outbox, positions, runtime, views, webhooks

trello_router = APIRouter(prefix="/api/trello", tags=["trello"])

LABEL_COLOURS = {"green", "yellow", "orange", "red", "purple", "blue", "sky", "lime", "pink",
                 "black"} | {f"{c}_{v}" for c in ("green", "yellow", "orange", "red", "purple",
                                                   "blue", "sky", "lime", "pink", "black")
                             for v in ("dark", "light")}
_CARD_FIELDS = ("name", "desc", "start", "due", "due_complete", "closed")


class _Tx:
    """A connection that commits and wakes the outbox on success."""

    def __enter__(self):
        self.conn = get_connection()
        return self.conn

    def __exit__(self, exc_type, *_):
        try:
            if exc_type is None:
                self.conn.commit()
                runtime.kick_outbox()
            else:
                self.conn.rollback()
        finally:
            self.conn.close()


def _need(conn, otype: str, object_id: str) -> dict:
    row = mirror.get(conn, otype, object_id)
    if row is None or row.get("removed_at"):
        raise HTTPException(status_code=404, detail="Not found.")
    return row


def _confirm(confirm: int) -> None:
    if not confirm:
        raise HTTPException(status_code=400,
                            detail="Deleting cannot be undone in Trello. Confirm to go ahead.")


def _text(body: dict, key: str, limit: int, required: bool = True) -> str:
    v = str(body.get(key) or "").strip()
    if required and not v:
        raise HTTPException(status_code=400, detail=f"A {key} is needed.")
    return v[:limit]


def _neighbour_pos(conn, table: str, before_id, after_id) -> float:
    def pos(oid):
        if not oid:
            return None
        r = conn.execute(f"SELECT pos FROM {table} WHERE id = ?", (oid,)).fetchone()
        return r["pos"] if r else None
    return positions.between(pos(before_id), pos(after_id))


def _last_pos(conn, table: str, col: str, value: str) -> float:
    last = conn.execute(f"SELECT MAX(pos) FROM {table} WHERE {col} = ? AND removed_at IS NULL",
                        (value,)).fetchone()[0]
    return positions.between(last, None)


# ── Connection ───────────────────────────────────────────────────────────────

def _config_payload() -> dict:
    cfg = mapping.get_config()
    key, token = mapping.credentials()
    is_owner, owner = mapping.owner_state()
    conn = get_connection()
    try:
        links = len(tq.list_links(conn))
    finally:
        conn.close()
    return {"has_credentials": bool(key and token), "has_secret": bool(mapping.secret()),
            "member": cfg["member"] or None, "poll_seconds": cfg["poll_seconds"],
            "webhooks": cfg["webhooks"], "mode": mapping.mode(),
            "commission_board_id": cfg["commission_board_id"],
            "list_status": cfg["list_status"], "statuses": list(cq.STATUSES),
            "is_owner": is_owner, "owner_tag": owner,
            "legacy_005": {"links": links if (cfg.get("board_id") or cfg.get("list_map")) else 0,
                           "migrated": bool(cfg["migrated_005"])}}


@trello_router.get("/config")
def get_config():
    return _config_payload()


@trello_router.put("/config")
def put_config(body: dict):
    fields = {}
    if "poll_seconds" in body:
        try:
            fields["poll_seconds"] = max(mapping.MIN_POLL, int(body["poll_seconds"]))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Poll interval must be a number of seconds.")
    if "commission_board_id" in body:
        fields["commission_board_id"] = str(body.get("commission_board_id") or "")
    if "list_status" in body:
        ls = body.get("list_status") or {}
        if not isinstance(ls, dict):
            raise HTTPException(status_code=400, detail="list_status must map list ids to statuses.")
        fields["list_status"] = {str(k): v for k, v in ls.items() if v in cq.STATUSES}
    if not fields:
        raise HTTPException(status_code=400, detail="Nothing to change.")
    mapping.save_config(**fields)
    return _config_payload()


@trello_router.post("/test")
def test_credentials(body: dict):
    """Whose credentials these are. Saves nothing, writes nothing to Trello."""
    key, token = str(body.get("key") or ""), str(body.get("token") or "")
    if not (key and token):
        key, token = mapping.credentials()
    try:
        me = TrelloClient(key, token).me()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except TrelloError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "member": me}


@trello_router.post("/credentials")
def save_credentials(body: dict):
    """Store key / token / secret. All three are vaulted (config.CREDENTIAL_FIELDS)
    and scrubbed from logs; nothing is echoed back. Connecting claims the Trello
    connection for this instance and starts an import."""
    update = {}
    for field, setting in (("key", "trello_api_key"), ("token", "trello_token"),
                           ("secret", "trello_secret")):
        v = str(body.get(field) or "").strip()
        if v:
            update[setting] = v
    if not update:
        raise HTTPException(status_code=400, detail="Nothing to save.")
    config.save_settings(update)
    if "trello_api_key" in update or "trello_token" in update:
        mapping.save_config(member={})              # re-learn whose account this is
    if not mapping.get_config()["owner_tag"]:
        mapping.claim()
    runtime.state["force_all"] = True
    runtime.poll_wake.set()
    key, token = mapping.credentials()
    return {"saved": True, "has_credentials": bool(key and token),
            "has_secret": bool(mapping.secret())}


def _status_payload() -> dict:
    conn = get_connection()
    try:
        return {"connected": mapping.is_configured(), "is_owner": mapping.owner_state()[0],
                "mode": mapping.mode(), "poll_seconds": mapping.get_config()["poll_seconds"],
                "import": dict(runtime.state["import"]), "outbox": outbox.counts(conn),
                "failed": outbox.failed(conn),
                "conflicts": conn.execute(
                    "SELECT COUNT(*) FROM trello_mirror_conflicts").fetchone()[0],
                "last_poll_at": runtime.state["last_poll_at"],
                "last_error": runtime.state["last_error"]}
    finally:
        conn.close()


@trello_router.get("/status")
def status():
    return _status_payload()


@trello_router.post("/import")
def start_import(body: dict | None = None):
    """Read everything now. ``confirm_removals`` lets the next pass through the
    mass-removal guard once, after the operator has checked the board."""
    if (body or {}).get("confirm_removals"):
        runtime.state["confirm_removals"] = True
    runtime.state["force_all"] = True
    runtime.poll_wake.set()
    return _status_payload()


@trello_router.post("/outbox/{seq}/dismiss")
def dismiss(seq: int):
    with _Tx() as conn:
        if not outbox.dismiss(conn, seq):
            raise HTTPException(status_code=404, detail="No such failed change.")
    return {"ok": True}


# ── Webhooks ─────────────────────────────────────────────────────────────────

def _callback(request: Request) -> str:
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    return f"{proto}://{host}/hooks/trello"


@trello_router.post("/webhooks/enable")
def enable_webhooks(request: Request):
    if not mapping.secret():
        raise HTTPException(status_code=400,
                            detail="Live updates need the Secret from your Trello app page.")
    callback = _callback(request)
    if not callback.startswith("https://"):
        return {"ok": False, "registered": 0, "callback": callback,
                "error": "Live updates need this server to be reachable over https."}
    mapping.save_config(webhooks="auto", webhook_callback=callback)
    conn = get_connection()
    try:
        made = webhooks.register_missing(conn, mapping.client_from_settings())
        total = conn.execute("SELECT COUNT(*) FROM trello_boards WHERE webhook_id != ''").fetchone()[0]
    except TrelloError as e:
        return {"ok": False, "registered": 0, "callback": callback, "error": str(e)}
    finally:
        conn.close()
    if not total:
        return {"ok": False, "registered": 0, "callback": callback,
                "error": "Trello could not reach this server to confirm the address."}
    return {"ok": True, "registered": made, "callback": callback}


@trello_router.post("/webhooks/disable")
def disable_webhooks():
    conn = get_connection()
    try:
        try:
            webhooks.remove_all(conn, mapping.client_from_settings())
        except (TrelloError, ValueError):
            pass
    finally:
        conn.close()
    mapping.save_config(webhooks="off")
    return {"ok": True}


# ── Boards and lists ─────────────────────────────────────────────────────────

@trello_router.get("/boards")
def list_boards(include_hidden: int = 0, include_removed: int = 0):
    conn = get_connection()
    try:
        return {"boards": views.boards(conn, bool(include_hidden), bool(include_removed))}
    finally:
        conn.close()


@trello_router.get("/boards/{board_id}")
def get_board(board_id: str, include_archived: int = 0, include_removed: int = 0):
    conn = get_connection()
    try:
        b = views.board(conn, board_id, bool(include_archived), bool(include_removed))
    finally:
        conn.close()
    if b is None:
        raise HTTPException(status_code=404, detail="No such board.")
    return b


@trello_router.patch("/boards/{board_id}")
def patch_board(board_id: str, body: dict):
    """PawPoller-only: hide a board from the picker (FR-004). No Trello write."""
    with _Tx() as conn:
        _need(conn, "board", board_id)
        conn.execute("UPDATE trello_boards SET hidden = ? WHERE id = ?",
                     (1 if body.get("hidden") else 0, board_id))
    return {"ok": True}


@trello_router.post("/boards/{board_id}/lists")
def create_list(board_id: str, body: dict):
    name = _text(body, "name", 512)
    with _Tx() as conn:
        _need(conn, "board", board_id)
        lid = mirror.tmp_id()
        pos = _last_pos(conn, "trello_lists", "board_id", board_id)
        mirror.insert_local(conn, "list", lid, {"board_id": board_id, "name": name, "pos": pos})
        outbox.enqueue(conn, "list.create", "list", lid, {"name": name, "pos": pos})
    return {"id": lid, "name": name, "pos": pos, "closed": False}


@trello_router.patch("/lists/{list_id}")
def patch_list(list_id: str, body: dict):
    fields = {}
    if "name" in body:
        fields["name"] = _text(body, "name", 512)
    if "closed" in body:
        fields["closed"] = bool(body["closed"])
    if not fields:
        raise HTTPException(status_code=400, detail="Nothing to change.")
    with _Tx() as conn:
        _need(conn, "list", list_id)
        mirror.set_local(conn, "list", list_id, fields)
        outbox.enqueue(conn, "list.update", "list", list_id, fields)
    return {"ok": True}


@trello_router.post("/lists/{list_id}/move")
def move_list(list_id: str, body: dict):
    with _Tx() as conn:
        _need(conn, "list", list_id)
        pos = _neighbour_pos(conn, "trello_lists", body.get("before_id"), body.get("after_id"))
        mirror.set_local(conn, "list", list_id, {"pos": pos})
        outbox.enqueue(conn, "list.move", "list", list_id, {"pos": pos})
    return {"ok": True, "pos": pos}


# ── Cards ────────────────────────────────────────────────────────────────────

@trello_router.post("/lists/{list_id}/cards")
def create_card(list_id: str, body: dict):
    name = _text(body, "name", 16384)
    with _Tx() as conn:
        lst = _need(conn, "list", list_id)
        cid = mirror.tmp_id()
        pos = _last_pos(conn, "trello_cards", "list_id", list_id)
        mirror.insert_local(conn, "card", cid, {"board_id": lst["board_id"], "list_id": list_id,
                                                "name": name, "pos": pos})
        outbox.enqueue(conn, "card.create", "card", cid, {"name": name, "pos": pos,
                                                          "list_id": list_id})
    return {"id": cid, "list_id": list_id, "name": name, "pos": pos, "pending": True}


@trello_router.get("/cards/{card_id}")
def get_card(card_id: str):
    conn = get_connection()
    try:
        c = views.card(conn, card_id)
    finally:
        conn.close()
    if c is None:
        raise HTTPException(status_code=404, detail="No such card.")
    if not mirror.is_tmp(card_id):
        # The only way to see comment edits/deletions when polling (research R2).
        runtime.mark_card_dirty(card_id, c["card"]["board_id"])
    return c


@trello_router.patch("/cards/{card_id}")
def patch_card(card_id: str, body: dict):
    fields = {}
    for k in _CARD_FIELDS:
        if k not in body:
            continue
        v = body[k]
        if k == "name":
            v = _text(body, "name", 16384)
        elif k == "desc":
            v = str(v or "")[:16384]
        elif k in ("due_complete", "closed"):
            v = bool(v)
        else:
            v = str(v) if v else None
        fields[k] = v
    if not fields:
        raise HTTPException(status_code=400, detail="Nothing to change.")
    with _Tx() as conn:
        card = _need(conn, "card", card_id)
        mirror.set_local(conn, "card", card_id, fields)
        outbox.enqueue(conn, "card.update", "card", card_id, fields)
        link = tq.get_link_by_card(conn, card_id)
    if link and ("closed" in fields or "due" in fields):
        conn2 = get_connection()
        try:
            commission.sync_from_board(conn2, card["board_id"])
        finally:
            conn2.close()
    return {"ok": True}


@trello_router.post("/cards/{card_id}/move")
def move_card(card_id: str, body: dict):
    list_id = str(body.get("list_id") or "")
    with _Tx() as conn:
        card = _need(conn, "card", card_id)
        lst = _need(conn, "list", list_id)
        if lst["board_id"] != card["board_id"]:
            raise HTTPException(status_code=400, detail="Cards move within their own board.")
        before, after = body.get("before_id"), body.get("after_id")
        if not before and not after:
            pos = _last_pos(conn, "trello_cards", "list_id", list_id)
        else:
            pos = _neighbour_pos(conn, "trello_cards", before, after)
        mirror.set_local(conn, "card", card_id, {"list_id": list_id, "pos": pos})
        outbox.enqueue(conn, "card.move", "card", card_id, {"list_id": list_id, "pos": pos})
    conn = get_connection()
    try:
        if tq.get_link_by_card(conn, card_id):
            commission.sync_from_board(conn, card["board_id"])
    finally:
        conn.close()
    return {"ok": True, "pos": pos}


def _set_labels(card_id: str, change) -> dict:
    with _Tx() as conn:
        card = _need(conn, "card", card_id)
        labels = sorted(change(set(card["labels"] or [])))
        mirror.set_local(conn, "card", card_id, {"labels": labels})
        outbox.enqueue(conn, "card.labels", "card", card_id, {"labels": labels})
    return {"ok": True, "labels": labels}


@trello_router.post("/cards/{card_id}/labels")
def add_card_label(card_id: str, body: dict):
    label_id = str(body.get("label_id") or "")
    conn = get_connection()
    try:
        _need(conn, "label", label_id)
    finally:
        conn.close()
    return _set_labels(card_id, lambda s: s | {label_id})


@trello_router.delete("/cards/{card_id}/labels/{label_id}")
def remove_card_label(card_id: str, label_id: str):
    return _set_labels(card_id, lambda s: s - {label_id})


@trello_router.put("/cards/{card_id}/cover")
def set_cover(card_id: str, body: dict):
    cover = {}
    if body.get("color"):
        if body["color"] not in COVER_COLOURS:
            raise HTTPException(status_code=400, detail="Unknown cover colour.")
        cover = {"color": body["color"],
                 "size": "full" if body.get("size") == "full" else "normal"}
        if body.get("brightness") in ("dark", "light"):
            cover["brightness"] = body["brightness"]
    with _Tx() as conn:
        _need(conn, "card", card_id)
        mirror.set_local(conn, "card", card_id, {"cover": cover})
        outbox.enqueue(conn, "card.cover", "card", card_id, {"cover": cover})
    return {"ok": True, "cover": cover or None}


@trello_router.post("/cards/{card_id}/cover")
async def upload_cover(card_id: str, file: UploadFile = File(...)):
    conn = get_connection()
    try:
        _need(conn, "card", card_id)            # before anything touches the disk
    finally:
        conn.close()
    content = await file.read(covers.MAX_UPLOAD + 1)
    try:
        staged = covers.stage(content, file.content_type or "")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    name = (file.filename or "cover")[:120]
    with _Tx() as conn:
        _need(conn, "card", card_id)
        outbox.enqueue(conn, "cover.upload", "card", card_id,
                       {"staged": staged, "file_name": name, "mime": file.content_type})
    return {"ok": True, "pending": True}


@trello_router.get("/covers/{attachment_id}")
def get_cover(attachment_id: str):
    path = covers.path_for(attachment_id)
    if path is None or not path.is_file():
        raise HTTPException(status_code=404, detail="No such cover.")
    return FileResponse(path, media_type=covers.MEDIA_TYPES.get(path.suffix.lower(),
                                                                "application/octet-stream"),
                        headers={"Cache-Control": "private, max-age=3600"})


# ── Labels ───────────────────────────────────────────────────────────────────

def _colour(v):
    if v in (None, "", "none"):
        return None
    if v not in LABEL_COLOURS:
        raise HTTPException(status_code=400, detail="Unknown label colour.")
    return v


@trello_router.post("/boards/{board_id}/labels")
def create_label(board_id: str, body: dict):
    name = _text(body, "name", 16384, required=False)
    color = _colour(body.get("color"))
    if not name and not color:
        raise HTTPException(status_code=400, detail="A label needs a name or a colour.")
    with _Tx() as conn:
        _need(conn, "board", board_id)
        lid = mirror.tmp_id()
        mirror.insert_local(conn, "label", lid, {"board_id": board_id, "name": name,
                                                 "color": color or ""})
        outbox.enqueue(conn, "label.create", "label", lid, {"name": name, "color": color or ""})
    return {"id": lid, "name": name, "color": color}


@trello_router.patch("/labels/{label_id}")
def patch_label(label_id: str, body: dict):
    fields = {}
    if "name" in body:
        fields["name"] = _text(body, "name", 16384, required=False)
    if "color" in body:
        fields["color"] = _colour(body.get("color")) or ""
    if not fields:
        raise HTTPException(status_code=400, detail="Nothing to change.")
    with _Tx() as conn:
        _need(conn, "label", label_id)
        mirror.set_local(conn, "label", label_id, fields)
        outbox.enqueue(conn, "label.update", "label", label_id, fields)
    return {"ok": True}


@trello_router.delete("/labels/{label_id}")
def delete_label(label_id: str, confirm: int = 0):
    _confirm(confirm)
    with _Tx() as conn:
        lb = _need(conn, "label", label_id)
        mirror.mark_removed(conn, "label", label_id)
        for r in conn.execute("SELECT id, labels FROM trello_cards WHERE board_id = ? "
                              "AND labels LIKE ?", (lb["board_id"], f"%{label_id}%")).fetchall():
            c = mirror.row_dict("card", r)
            mirror.set_local(conn, "card", c["id"],
                             {"labels": [x for x in c["labels"] if x != label_id]})
            # Trello drops the label from its cards itself; agree on that now.
            mirror.advance_agreed(conn, "card", c["id"],
                                  {"labels": [x for x in c["agreed"].get("labels", c["labels"])
                                              if x != label_id]})
        outbox.enqueue(conn, "label.delete", "label", label_id, {})
    return {"ok": True}


# ── Checklists ───────────────────────────────────────────────────────────────

@trello_router.post("/cards/{card_id}/checklists")
def create_checklist(card_id: str, body: dict):
    name = _text(body, "name", 16384)
    with _Tx() as conn:
        card = _need(conn, "card", card_id)
        cl = mirror.tmp_id()
        pos = _last_pos(conn, "trello_checklists", "card_id", card_id)
        mirror.insert_local(conn, "checklist", cl, {"board_id": card["board_id"],
                                                    "card_id": card_id, "name": name, "pos": pos})
        outbox.enqueue(conn, "checklist.create", "checklist", cl, {"name": name, "pos": pos})
    return {"id": cl, "name": name, "pos": pos, "items": []}


@trello_router.patch("/checklists/{checklist_id}")
def patch_checklist(checklist_id: str, body: dict):
    name = _text(body, "name", 16384)
    with _Tx() as conn:
        _need(conn, "checklist", checklist_id)
        mirror.set_local(conn, "checklist", checklist_id, {"name": name})
        outbox.enqueue(conn, "checklist.update", "checklist", checklist_id, {"name": name})
    return {"ok": True}


@trello_router.delete("/checklists/{checklist_id}")
def delete_checklist(checklist_id: str, confirm: int = 0):
    _confirm(confirm)
    with _Tx() as conn:
        _need(conn, "checklist", checklist_id)
        mirror.mark_removed(conn, "checklist", checklist_id)
        conn.execute("UPDATE trello_check_items SET removed_at = ? WHERE checklist_id = ?",
                     (mirror.now(), checklist_id))
        outbox.enqueue(conn, "checklist.delete", "checklist", checklist_id, {})
    return {"ok": True}


@trello_router.post("/checklists/{checklist_id}/items")
def create_item(checklist_id: str, body: dict):
    name = _text(body, "name", 16384)
    with _Tx() as conn:
        cl = _need(conn, "checklist", checklist_id)
        iid = mirror.tmp_id()
        pos = _last_pos(conn, "trello_check_items", "checklist_id", checklist_id)
        mirror.insert_local(conn, "checkitem", iid, {"board_id": cl["board_id"],
                                                     "checklist_id": checklist_id,
                                                     "name": name, "pos": pos})
        outbox.enqueue(conn, "checkitem.create", "checkitem", iid, {"name": name, "pos": pos})
    return {"id": iid, "name": name, "pos": pos, "state": "incomplete"}


@trello_router.patch("/items/{item_id}")
def patch_item(item_id: str, body: dict):
    fields = {}
    if "name" in body:
        fields["name"] = _text(body, "name", 16384)
    if "state" in body:
        fields["state"] = "complete" if body["state"] == "complete" else "incomplete"
    with _Tx() as conn:
        _need(conn, "checkitem", item_id)
        if "before_id" in body or "after_id" in body:
            fields["pos"] = _neighbour_pos(conn, "trello_check_items",
                                           body.get("before_id"), body.get("after_id"))
        if not fields:
            raise HTTPException(status_code=400, detail="Nothing to change.")
        mirror.set_local(conn, "checkitem", item_id, fields)
        outbox.enqueue(conn, "checkitem.update", "checkitem", item_id, fields)
    return {"ok": True}


@trello_router.delete("/items/{item_id}")
def delete_item(item_id: str, confirm: int = 0):
    _confirm(confirm)
    with _Tx() as conn:
        _need(conn, "checkitem", item_id)
        mirror.mark_removed(conn, "checkitem", item_id)
        outbox.enqueue(conn, "checkitem.delete", "checkitem", item_id, {})
    return {"ok": True}


# ── Comments ─────────────────────────────────────────────────────────────────

@trello_router.post("/cards/{card_id}/comments")
def create_comment(card_id: str, body: dict):
    text = _text(body, "text", 16384)
    member = mapping.get_config()["member"] or {}
    with _Tx() as conn:
        card = _need(conn, "card", card_id)
        cid = mirror.tmp_id()
        mirror.insert_local(conn, "comment", cid, {
            "board_id": card["board_id"], "card_id": card_id, "text": text, "date": mirror.now(),
            "author_id": member.get("id", ""), "author_name": member.get("full_name", "")})
        outbox.enqueue(conn, "comment.create", "comment", cid, {"text": text})
    return {"id": cid, "text": text, "pending": True, "is_mine": True}


def _own_comment(conn, comment_id: str) -> dict:
    cm = _need(conn, "comment", comment_id)
    me = (mapping.get_config()["member"] or {}).get("id", "")
    if not (mirror.is_tmp(comment_id) or (me and cm["author_id"] == me)):
        raise HTTPException(status_code=403, detail="Only your own comments can be changed.")
    return cm


@trello_router.patch("/comments/{comment_id}")
def patch_comment(comment_id: str, body: dict):
    text = _text(body, "text", 16384)
    with _Tx() as conn:
        _own_comment(conn, comment_id)
        mirror.set_local(conn, "comment", comment_id, {"text": text})
        outbox.enqueue(conn, "comment.update", "comment", comment_id, {"text": text})
    return {"ok": True}


@trello_router.delete("/comments/{comment_id}")
def delete_comment(comment_id: str, confirm: int = 0):
    _confirm(confirm)
    with _Tx() as conn:
        _own_comment(conn, comment_id)
        mirror.mark_removed(conn, "comment", comment_id)
        outbox.enqueue(conn, "comment.delete", "comment", comment_id, {})
    return {"ok": True}


# ── Conflicts ────────────────────────────────────────────────────────────────

@trello_router.get("/conflicts")
def list_conflicts():
    conn = get_connection()
    try:
        return {"conflicts": views.conflicts(conn)}
    finally:
        conn.close()


@trello_router.post("/conflicts/resolve")
def resolve_conflict(body: dict):
    """Choose a side for ONE field (FR-020). There is deliberately no resolve-all."""
    import json
    otype, oid = str(body.get("object_type") or ""), str(body.get("object_id") or "")
    field, keep = str(body.get("field") or ""), str(body.get("keep") or "")
    if keep not in ("pawpoller", "trello") or otype not in mirror.TYPES:
        raise HTTPException(status_code=400, detail="Choose 'pawpoller' or 'trello'.")
    with _Tx() as conn:
        row = conn.execute("SELECT * FROM trello_mirror_conflicts WHERE object_type = ? AND "
                           "object_id = ? AND field = ?", (otype, oid, field)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="No such conflict.")
        remote = json.loads(row["remote_value"] or "null")
        # Trello's value is now the agreed one either way: keeping ours means the
        # held op sends over it; keeping Trello's means ours is dropped.
        mirror.advance_agreed(conn, otype, oid, {field: remote})
        held = conn.execute("SELECT seq, fields FROM trello_outbox WHERE object_type = ? AND "
                            "object_id = ? AND state = 'held'", (otype, oid)).fetchall()
        if keep == "trello":
            mirror.set_local(conn, otype, oid, {field: remote})
            for h in held:
                f = json.loads(h["fields"] or "{}")
                f.pop(field, None)
                if f:
                    conn.execute("UPDATE trello_outbox SET fields = ? WHERE seq = ?",
                                 (json.dumps(f, sort_keys=True), h["seq"]))
                else:
                    conn.execute("DELETE FROM trello_outbox WHERE seq = ?", (h["seq"],))
        conn.execute("DELETE FROM trello_mirror_conflicts WHERE id = ?", (row["id"],))
        still = {r["field"] for r in conn.execute(
            "SELECT field FROM trello_mirror_conflicts WHERE object_type = ? AND object_id = ?",
            (otype, oid))}
        if not still:
            conn.execute("UPDATE trello_outbox SET state = 'pending' WHERE object_type = ? AND "
                         "object_id = ? AND state = 'held'", (otype, oid))
    return {"resolved": True, "field": field, "keep": keep}


# ── Commissions ──────────────────────────────────────────────────────────────

@trello_router.post("/cards/{card_id}/commission")
def mark_commission(card_id: str, body: dict):
    try:
        price = float(body.get("price") or 0)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Price must be a number.")
    conn = get_connection()
    try:
        _need(conn, "card", card_id)
        return commission.mark(conn, card_id, client_name=str(body.get("client_name") or "").strip(),
                               price=price, currency=str(body.get("currency") or "USD")[:8])
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    finally:
        conn.close()


@trello_router.delete("/cards/{card_id}/commission")
def unmark_commission(card_id: str):
    conn = get_connection()
    try:
        if not commission.unmark(conn, card_id):
            raise HTTPException(status_code=404, detail="That card is not a commission.")
        return {"ok": True}
    finally:
        conn.close()


@trello_router.get("/migrate-005")
def migration_preview():
    conn = get_connection()
    try:
        plan = commission.migration_plan(conn)
    finally:
        conn.close()
    return {"strip": plan["strip"],
            "create": [{"client_name": c["client_name"], "status": c["status"]}
                       for c in plan["create"]],
            "list_status": plan["list_status"], "board_id": plan["board_id"]}


@trello_router.post("/migrate-005")
def migration_apply(body: dict):
    if not body.get("confirm"):
        raise HTTPException(status_code=400, detail="Preview first, then confirm.")
    conn = get_connection()
    try:
        result = commission.migrate(conn)
    finally:
        conn.close()
    runtime.kick_outbox()
    return result
