"""Trello webhooks: one per board, telling the server a board changed (R3).

A delivery is only ever a HINT. It marks the board (or, for a comment edit or
deletion, the card) dirty and wakes the poller; the board read decides what the
values are. One code path decides values, whatever woke it.

⚠ Needs the app **Secret** — Trello signs each delivery with it, and an unsigned
or wrongly signed delivery is refused.
"""
from __future__ import annotations

import logging

from clients.trello.client import TrelloError
from trello import mapping

logger = logging.getLogger(__name__)

# Comment edits and deletions are "excluded" action types: the actions endpoints
# never return them, only webhooks do (research R2). The card is re-read for them.
COMMENT_ACTIONS = {"commentCard", "updateComment", "deleteComment"}


def register_missing(conn, client) -> int:
    """Give every live, non-Inbox board a webhook (Inbox webhooks never fire)."""
    if mapping.mode() != "webhook":
        return 0
    callback = mapping.get_config()["webhook_callback"]
    made = 0
    for r in conn.execute("SELECT id FROM trello_boards WHERE (webhook_id IS NULL OR "
                          "webhook_id = '') AND removed_at IS NULL AND closed = 0 "
                          "AND is_inbox = 0 AND imported_at IS NOT NULL").fetchall():
        try:
            hook = client.create_webhook(r["id"], callback)
        except TrelloError as e:
            # Most often: Trello's HEAD check could not reach the callback.
            logger.warning("Trello webhook not registered: %s", e)
            continue
        conn.execute("UPDATE trello_boards SET webhook_id = ? WHERE id = ?",
                     (hook.get("id", ""), r["id"]))
        made += 1
    conn.commit()
    return made


def remove_all(conn, client) -> int:
    """Delete every webhook this token holds for our callback, and forget them."""
    gone = 0
    callback = mapping.get_config()["webhook_callback"]
    try:
        hooks = client.list_webhooks()
    except TrelloError:
        hooks = []
    for h in hooks:
        if callback and h.get("callbackURL") != callback:
            continue
        try:
            client.delete_webhook(h["id"])
            gone += 1
        except TrelloError:
            pass
    conn.execute("UPDATE trello_boards SET webhook_id = ''")
    conn.commit()
    return gone


def handle(payload: dict, runtime) -> None:
    """Turn one verified delivery into dirty marks."""
    action = payload.get("action") or {}
    data = action.get("data") or {}
    board_id = (data.get("board") or {}).get("id") or (payload.get("model") or {}).get("id", "")
    if action.get("type") in COMMENT_ACTIONS:
        card_id = (data.get("card") or {}).get("id", "")
        runtime.mark_card_dirty(card_id, board_id)
    runtime.mark_board_dirty(board_id)
