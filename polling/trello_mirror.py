"""The Trello mirror's two threads (spec 006, research R3/R4/R11).

* ``run_trello_mirror`` — reads. Every ``poll_seconds`` (or at once when a
  webhook or "refresh now" wakes it) it asks Trello for the member's boards in one
  request, re-reads only the boards whose activity moved or that were marked
  dirty, and sweeps every board every 15 minutes regardless.
* ``run_trello_outbox`` — writes. Drains the outbox in order whenever a route
  queues an edit, pausing when Trello says wait.

Both are in ``main.py`` AND ``server.py``'s thread tables (the 4.36.1 lesson —
a scheduler wired into one entry point only runs on half the installs). A
connected desktop runs neither: it has no database and shows the server's page.
"""
from __future__ import annotations

import logging
import time

from clients.trello.client import TrelloAuthError, TrelloError
from database.db import get_connection
from trello import commission, covers, mapping, mirror, outbox, runtime, webhooks

logger = logging.getLogger(__name__)

STARTUP_DELAY = 20
SWEEP_SECONDS = 15 * 60
DEBOUNCE = 2.0


def _ready() -> bool:
    return mapping.is_configured() and mapping.owner_state()[0]


def sync_board(conn, client, board_id: str, *, confirm_removals: bool = False) -> dict:
    """Read one board and apply it. Raises on a failed read — nothing is written."""
    row = conn.execute("SELECT comment_cursor, imported_at FROM trello_boards WHERE id = ?",
                       (board_id,)).fetchone()
    read = client.board_full(board_id)
    since = (row["comment_cursor"] if row else "") or ""
    comments = client.board_comments(board_id, since=since)
    try:
        stats = mirror.apply_board(conn, read, comments, confirm_removals=confirm_removals)
        if comments:
            conn.execute("UPDATE trello_boards SET comment_cursor = ? WHERE id = ?",
                         (comments[0].get("id", ""), board_id))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    if board_id == mapping.get_config()["commission_board_id"]:
        commission.sync_from_board(conn, board_id)
    return stats


def tick(force_all: bool = False) -> None:
    """One read pass. Every failure is reported, none kills the thread."""
    client = mapping.client_from_settings()
    conn = get_connection()
    try:
        cfg = mapping.get_config()
        if not cfg["member"].get("id"):
            me = client.me()
            mapping.save_config(member=me, inbox_id=client.inbox_board_id())
            cfg = mapping.get_config()
        # "-" = Trello names an Inbox but will not let this token read it (see below).
        inbox = "" if cfg["inbox_id"] == "-" else cfg["inbox_id"]

        result = mirror.apply_member_boards(conn, client.member_boards(),
                                            extra_ids={inbox} if inbox else set())
        if inbox and not mirror.get(conn, "board", inbox):
            mirror.insert_local(conn, "board", inbox, {"name": "Inbox"})
            conn.execute("UPDATE trello_boards SET is_inbox = 1, agreed = '{}' WHERE id = ?",
                         (inbox,))
        conn.commit()

        dirty_boards, dirty_cards = runtime.take_dirty()
        force_all = force_all or bool(runtime.state.pop("force_all", False))
        confirm = bool(runtime.state.pop("confirm_removals", False))
        boards = conn.execute(
            "SELECT id, last_activity, imported_at FROM trello_boards "
            "WHERE removed_at IS NULL AND closed = 0").fetchall()
        todo = [b["id"] for b in boards
                if force_all or b["id"] in dirty_boards or not b["imported_at"]
                or (result["activity"].get(b["id"]) or "") != (b["last_activity"] or "")
                or b["id"] == inbox]
        runtime.state["import"] = {"done": sum(1 for b in boards if b["imported_at"]),
                                   "total": len(boards), "running": bool(todo)}
        for bid in todo:
            try:
                sync_board(conn, client, bid, confirm_removals=confirm)
            except mirror.MassRemoval as e:
                runtime.set_error(str(e))
                logger.warning("Trello mirror: %s", e)
            except TrelloAuthError:
                # ⚠ Not "the credentials are wrong": the member call above just
                # succeeded with them. A 401 on ONE board means that board is not
                # readable with this token — observed live for Trello's personal
                # Inbox (research R3, S-I1). Raising here aborted the whole pass,
                # so covers and every later board never ran, and Settings said the
                # key was bad.
                if bid == inbox:
                    mapping.save_config(inbox_id="-")
                    mirror.mark_removed(conn, "board", bid)
                    conn.commit()
                    logger.info("Trello Inbox is not readable with this token; skipped.")
                else:
                    logger.info("A Trello board refused this token; skipped.")
            except TrelloError as e:
                logger.info("Trello board read skipped: %s", e)
                runtime.mark_board_dirty(bid)          # try again next pass
            runtime.state["import"]["done"] = conn.execute(
                "SELECT COUNT(*) FROM trello_boards WHERE imported_at IS NOT NULL "
                "AND removed_at IS NULL AND closed = 0").fetchone()[0]

        for card_id, bid in dirty_cards.items():
            try:
                mirror.apply_card_comments(conn, bid, card_id, client.card_comments(card_id))
                conn.commit()
            except TrelloError as e:
                logger.info("Trello comments not re-read: %s", type(e).__name__)

        covers.fetch_missing(conn, client)
        webhooks.register_missing(conn, client)
        runtime.state["import"]["running"] = False
        runtime.state["last_poll_at"] = mirror.now()
        runtime.set_error("")
    finally:
        conn.close()


def run_trello_mirror() -> None:
    """Blocking daemon entry point — the read side."""
    time.sleep(STARTUP_DELAY)
    last_sweep = 0.0
    while True:
        woken = False
        try:
            if _ready():
                sweep = time.time() - last_sweep >= SWEEP_SECONDS
                tick(force_all=sweep)
                if sweep:
                    last_sweep = time.time()
        except TrelloAuthError as e:
            runtime.set_error(str(e))
            logger.warning("Trello mirror: the key and token were refused.")
        except Exception as e:
            # ⚠ Never let a bad pass kill the thread; a silently dead mirror looks
            # exactly like "nothing changed". Type only — messages can carry content.
            runtime.set_error(f"The last Trello check failed ({type(e).__name__}).")
            logger.warning("Trello mirror pass failed: %s", type(e).__name__)
        woken = runtime.poll_wake.wait(mapping.get_config()["poll_seconds"])
        runtime.poll_wake.clear()
        if woken:
            time.sleep(DEBOUNCE)            # let a burst of webhook deliveries settle


def drain(max_ops: int = 500) -> str:
    """Send queued edits until the queue is empty or Trello says wait."""
    if not _ready():
        return "idle"
    client = mapping.client_from_settings()
    conn = get_connection()
    try:
        result = "empty"
        for _ in range(max_ops):
            if runtime.outbox_paused():
                return "wait"
            result = outbox.drain_one(conn, client)
            if result in ("empty", "wait", "auth"):
                break
        return result
    finally:
        conn.close()


def run_trello_outbox() -> None:
    """Blocking daemon entry point — the write side."""
    time.sleep(5)
    while True:
        try:
            drain()
        except Exception as e:
            logger.warning("Trello outbox pass failed: %s", type(e).__name__)
        runtime.outbox_wake.wait(max(1.0, min(30.0, runtime.outbox_paused() or 10.0)))
        runtime.outbox_wake.clear()
