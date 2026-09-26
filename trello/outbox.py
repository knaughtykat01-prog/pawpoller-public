"""The outbox: PawPoller's edits, sent to Trello in the order they were made (R4).

A route never talks to Trello. It writes the mirror and appends an op here in the
same transaction, then wakes the worker (`polling/trello_mirror.py`), which
calls :func:`drain_one` until the queue is empty or Trello says wait. That is
what makes an edit both immediate and safe offline: the UI reads the mirror, and
the op survives until Trello accepts it.

Per-op outcome:

* success            → op deleted; ``agreed`` advances for exactly the op's fields.
* retryable (429, network, 5xx) → op stays, backs off; the whole drain pauses,
  because later ops may depend on this one and order is the guarantee (FR-017).
* auth refused       → nothing more is sent until the credentials change.
* 404 / other 4xx    → op marked ``failed`` with a reason the operator sees; the
  board is re-read so the mirror stops claiming what Trello refused (FR-022).
"""
from __future__ import annotations

import json
import logging
import time

from clients.trello.client import (TrelloAuthError, TrelloError, TrelloNotFound,
                                   TrelloRetryableError)
from trello import mirror, runtime

logger = logging.getLogger(__name__)

MAX_BACKOFF = 300


def enqueue(conn, op: str, otype: str, object_id: str, fields: dict) -> int:
    cur = conn.execute(
        "INSERT INTO trello_outbox (op, object_type, object_id, fields) VALUES (?, ?, ?, ?)",
        (op, otype, object_id, json.dumps(fields, sort_keys=True)))
    return cur.lastrowid


def counts(conn) -> dict:
    out = {"pending": 0, "held": 0, "failed": 0}
    for r in conn.execute("SELECT state, COUNT(*) AS n FROM trello_outbox GROUP BY state"):
        out[r["state"]] = r["n"]
    return out


def failed(conn) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT seq, op, object_type, object_id, error FROM trello_outbox "
        "WHERE state = 'failed' ORDER BY seq")]


def dismiss(conn, seq: int) -> bool:
    return conn.execute("DELETE FROM trello_outbox WHERE seq = ? AND state = 'failed'",
                        (seq,)).rowcount > 0


def pending_objects(conn) -> set:
    return {(r["object_type"], r["object_id"]) for r in conn.execute(
        "SELECT object_type, object_id FROM trello_outbox WHERE state IN ('pending', 'held')")}


def _next(conn):
    """The oldest sendable op. An op waits behind a HELD op on the same object —
    order per object is kept even while a conflict is open."""
    return conn.execute(
        "SELECT * FROM trello_outbox o WHERE state = 'pending' AND next_try_at <= ? "
        "AND NOT EXISTS (SELECT 1 FROM trello_outbox h WHERE h.state = 'held' "
        "  AND h.object_id = o.object_id AND h.seq < o.seq) "
        "ORDER BY seq LIMIT 1", (time.time(),)).fetchone()


def _need(conn, otype: str, object_id: str) -> dict:
    row = mirror.get(conn, otype, object_id)
    if row is None:
        raise ValueError("That item is no longer in the mirror.")
    return row


def _real(object_id: str) -> str:
    if mirror.is_tmp(object_id):
        raise ValueError("It depends on something Trello has not accepted.")
    return object_id


def _execute(conn, client, op: dict) -> dict:
    """Send one op. Returns values to write back onto the row (and agree)."""
    name, oid = op["op"], op["object_id"]
    f = json.loads(op["fields"] or "{}")

    if name == "card.create":
        card = _need(conn, "card", oid)
        r = client.create_card(id_list=_real(card["list_id"]), name=f.get("name", card["name"]),
                               pos=f.get("pos", "bottom"))
        mirror.rekey(conn, oid, r.get("id", ""))
        return {"_id": r.get("id"), "pos": r.get("pos"), "url": r.get("shortUrl", "")}
    if name == "card.update":
        m = {"name": "name", "desc": "desc", "start": "start", "due": "due",
             "due_complete": "dueComplete", "closed": "closed"}
        r = client.update_card(_real(oid), **{m[k]: v for k, v in f.items() if k in m})
        return {}
    if name == "card.move":
        r = client.update_card(_real(oid), idList=_real(f["list_id"]), pos=f.get("pos", "bottom"))
        return {"pos": r.get("pos")}
    if name == "card.labels":
        card = _need(conn, "card", oid)
        was = set(card["agreed"].get("labels") or [])
        want = set(f.get("labels") or [])
        for lid in sorted(want - was):
            try:
                client.add_label(_real(oid), _real(lid))
            except (TrelloNotFound, TrelloAuthError, TrelloRetryableError):
                raise
            except TrelloError:
                # Already on the card (a retry after a half-sent op). If it was a
                # real refusal, the next board read shows the card without it.
                pass
        for lid in sorted(was - want):
            try:
                client.remove_label(_real(oid), lid)
            except TrelloNotFound:
                pass                                # already off the card
        return {}
    if name == "card.cover":
        client.set_cover(_real(oid), {k: v for k, v in (f.get("cover") or {}).items()
                                      if k in ("color", "size", "brightness")})
        return {}
    if name == "cover.upload":
        from trello import covers
        path = covers.staged_path(f["staged"])
        r = client.upload_cover(_real(oid), f.get("file_name") or "cover",
                                path.read_bytes(), f.get("mime") or "image/png")
        att = (r[0] if isinstance(r, list) and r else r) or {}
        cover = {"attachment_id": att.get("id", ""), "size": "normal"}
        covers.adopt_staged(conn, f["staged"], att, oid)
        mirror.set_local(conn, "card", oid, {"cover": cover})
        mirror.advance_agreed(conn, "card", oid, {"cover": cover})
        return {}

    if name == "list.create":
        lst = _need(conn, "list", oid)
        r = client.create_list(_real(lst["board_id"]), f.get("name", lst["name"]),
                               pos=f.get("pos", "bottom"))
        mirror.rekey(conn, oid, r.get("id", ""))
        return {"_id": r.get("id"), "pos": r.get("pos")}
    if name in ("list.update", "list.move"):
        client.update_list(_real(oid), **{k: v for k, v in f.items()
                                          if k in ("name", "pos", "closed")})
        return {}

    if name == "label.create":
        lb = _need(conn, "label", oid)
        r = client.create_label(lb["board_id"], f.get("name", ""), f.get("color") or None)
        mirror.rekey(conn, oid, r.get("id", ""))
        return {"_id": r.get("id")}
    if name == "label.update":
        client.update_label(_real(oid), **{k: v for k, v in f.items() if k in ("name", "color")})
        return {}
    if name == "label.delete":
        try:
            client.delete_label(_real(oid))
        except TrelloNotFound:
            pass
        return {}

    if name == "checklist.create":
        cl = _need(conn, "checklist", oid)
        r = client.create_checklist(_real(cl["card_id"]), f.get("name", cl["name"]),
                                    pos=f.get("pos", "bottom"))
        mirror.rekey(conn, oid, r.get("id", ""))
        return {"_id": r.get("id"), "pos": r.get("pos")}
    if name == "checklist.update":
        client.update_checklist(_real(oid), **{k: v for k, v in f.items() if k in ("name", "pos")})
        return {}
    if name == "checklist.delete":
        try:
            client.delete_checklist(_real(oid))
        except TrelloNotFound:
            pass
        return {}

    if name == "checkitem.create":
        it = _need(conn, "checkitem", oid)
        r = client.create_check_item(_real(it["checklist_id"]), f.get("name", it["name"]),
                                     pos=f.get("pos", "bottom"))
        mirror.rekey(conn, oid, r.get("id", ""))
        return {"_id": r.get("id"), "pos": r.get("pos")}
    if name == "checkitem.update":
        it = _need(conn, "checkitem", oid)
        cl = _need(conn, "checklist", it["checklist_id"])
        client.update_check_item(_real(cl["card_id"]), _real(oid),
                                 **{k: v for k, v in f.items() if k in ("name", "state", "pos")})
        return {}
    if name == "checkitem.delete":
        it = _need(conn, "checkitem", oid)
        try:
            client.delete_check_item(_real(it["checklist_id"]), _real(oid))
        except TrelloNotFound:
            pass
        return {}

    if name == "comment.create":
        cm = _need(conn, "comment", oid)
        r = client.add_comment(_real(cm["card_id"]), f.get("text", cm["text"]))
        mirror.rekey(conn, oid, r.get("id", ""))
        return {"_id": r.get("id"), "date": r.get("date", "")}
    if name == "comment.update":
        client.edit_comment(_real(oid), f.get("text", ""))
        return {}
    if name == "comment.delete":
        try:
            client.delete_comment(_real(oid))
        except TrelloNotFound:
            pass
        return {}
    raise ValueError(f"Unknown change type {name}.")


def drain_one(conn, client) -> str:
    """Send the oldest sendable op. Returns ``sent`` / ``empty`` / ``wait`` / ``failed`` / ``auth``."""
    op = _next(conn)
    if op is None:
        return "empty"
    op = dict(op)
    otype, fields = op["object_type"], json.loads(op["fields"] or "{}")
    try:
        back = _execute(conn, client, op)
    except TrelloRetryableError as e:
        wait = e.retry_after or min(MAX_BACKOFF, 5 * 2 ** op["attempts"])
        conn.execute("UPDATE trello_outbox SET attempts = attempts + 1, next_try_at = ?, "
                     "error = ? WHERE seq = ?", (time.time() + wait, str(e), op["seq"]))
        conn.commit()
        runtime.pause_outbox(wait)
        return "wait"
    except TrelloAuthError as e:
        runtime.set_error(str(e))
        conn.rollback()
        return "auth"
    except (TrelloError, ValueError, KeyError, OSError) as e:
        conn.rollback()
        reason = str(e) if isinstance(e, (TrelloError, ValueError)) else "Could not send it."
        conn.execute("UPDATE trello_outbox SET state = 'failed', error = ? WHERE seq = ?",
                     (reason, op["seq"]))
        if op["op"].endswith(".create") and mirror.is_tmp(op["object_id"]):
            mirror.mark_removed(conn, otype, op["object_id"])
        row = mirror.get(conn, otype, op["object_id"])
        if row and row.get("board_id"):
            runtime.mark_board_dirty(row["board_id"])
        conn.commit()
        return "failed"

    oid = back.pop("_id", None) or op["object_id"]
    writeback = {k: v for k, v in back.items() if v not in (None, "")}
    if writeback:
        mirror.set_local(conn, otype, oid, writeback)
    agreed = {k: v for k, v in fields.items() if k in mirror.TYPES[otype][1]}
    agreed.update({k: v for k, v in writeback.items() if k in mirror.TYPES[otype][1]})
    if op["op"].endswith(".create"):
        row = mirror.get(conn, otype, oid)
        if row:
            agreed = {k: row.get(k) for k in mirror.TYPES[otype][1]}
    if not op["op"].endswith(".delete"):
        mirror.advance_agreed(conn, otype, oid, agreed)
    conn.execute("DELETE FROM trello_outbox WHERE seq = ?", (op["seq"],))
    conn.commit()
    return "sent"
