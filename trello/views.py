"""Read-only views of the mirror for the API. Every read is local — opening a
board never waits on Trello (plan: Performance Goals)."""
from __future__ import annotations

from database import trello_queries as tq
from trello import mapping, mirror, outbox


def _conflicted(conn) -> set:
    return {(r["object_type"], r["object_id"]) for r in conn.execute(
        "SELECT object_type, object_id FROM trello_mirror_conflicts")}


def boards(conn, include_hidden: bool = False, include_removed: bool = False) -> list[dict]:
    cfg = mapping.get_config()
    out = []
    for r in conn.execute("SELECT * FROM trello_boards ORDER BY is_inbox DESC, name COLLATE NOCASE"):
        if r["removed_at"] and not include_removed:
            continue
        if r["hidden"] and not include_hidden:
            continue
        if r["closed"] and not include_removed:
            continue
        out.append({"id": r["id"], "name": r["name"], "closed": bool(r["closed"]),
                    "hidden": bool(r["hidden"]), "is_inbox": bool(r["is_inbox"]),
                    "bg_color": r["bg_color"] or "", "bg_image_url": r["bg_image_url"] or "",
                    "url": r["url"] or "", "imported": bool(r["imported_at"]),
                    "removed": bool(r["removed_at"]),
                    "is_commission_board": r["id"] == cfg["commission_board_id"]})
    return out


def _cover_url(conn, cover: dict) -> str | None:
    att = (cover or {}).get("attachment_id")
    if not att:
        return None
    r = conn.execute("SELECT local_path FROM trello_covers WHERE attachment_id = ?",
                     (att,)).fetchone()
    return f"/api/trello/covers/{att}" if r and r["local_path"] not in (None, "", "-") else None


def board(conn, board_id: str, include_archived: bool = False,
          include_removed: bool = False) -> dict | None:
    b = mirror.get(conn, "board", board_id)
    if b is None:
        return None
    busy = outbox.pending_objects(conn)
    conflicted = _conflicted(conn)
    linked = {l["card_id"] for l in tq.list_links(conn)}

    def keep(r):
        return (include_removed or not r["removed_at"]) and (include_archived or not r["closed"])

    lists = [{"id": r["id"], "name": r["name"], "pos": r["pos"], "closed": bool(r["closed"])}
             for r in conn.execute("SELECT * FROM trello_lists WHERE board_id = ? "
                                   "ORDER BY pos, id", (board_id,)) if keep(r)]
    live_lists = {l["id"] for l in lists}
    checks: dict = {}
    for r in conn.execute(
            "SELECT c.card_id, i.state FROM trello_check_items i JOIN trello_checklists c "
            "ON c.id = i.checklist_id WHERE i.board_id = ? AND i.removed_at IS NULL "
            "AND c.removed_at IS NULL", (board_id,)):
        d = checks.setdefault(r["card_id"], {"done": 0, "total": 0})
        d["total"] += 1
        d["done"] += r["state"] == "complete"
    cards = []
    for r in conn.execute("SELECT * FROM trello_cards WHERE board_id = ? ORDER BY pos, id",
                          (board_id,)):
        if not keep(r) or r["list_id"] not in live_lists:
            continue
        c = mirror.row_dict("card", r)
        cards.append({
            "id": c["id"], "list_id": c["list_id"], "name": c["name"], "pos": c["pos"],
            "closed": bool(c["closed"]), "due": c["due"], "start": c["start"],
            "due_complete": bool(c["due_complete"]), "labels": c["labels"],
            "cover": c["cover"] or None, "cover_url": _cover_url(conn, c["cover"]),
            "has_desc": bool((c["desc"] or "").strip()), "comment_count": c["comment_count"],
            "checklist": checks.get(c["id"], {"done": 0, "total": 0}),
            "is_commission": c["id"] in linked,
            "pending": ("card", c["id"]) in busy or mirror.is_tmp(c["id"]),
            "conflict": ("card", c["id"]) in conflicted,
            "removed": bool(c["removed_at"])})
    labels = [{"id": r["id"], "name": r["name"], "color": r["color"] or None}
              for r in conn.execute("SELECT * FROM trello_labels WHERE board_id = ? "
                                    "AND removed_at IS NULL ORDER BY name", (board_id,))]
    return {"board": {"id": b["id"], "name": b["name"], "bg_color": b["bg_color"] or "",
                      "bg_image_url": b["bg_image_url"] or "", "url": b["url"] or "",
                      "hidden": bool(b["hidden"]), "is_inbox": bool(b["is_inbox"])},
            "lists": lists, "cards": cards, "labels": labels}


def conflicts(conn, object_type: str = "", object_ids: set | None = None) -> list[dict]:
    import json
    out = []
    for r in conn.execute("SELECT * FROM trello_mirror_conflicts ORDER BY detected_at DESC"):
        if object_type and r["object_type"] != object_type:
            continue
        if object_ids is not None and r["object_id"] not in object_ids:
            continue
        out.append({"object_type": r["object_type"], "object_id": r["object_id"],
                    "field": r["field"], "agreed": json.loads(r["agreed_value"] or "null"),
                    "local": json.loads(r["local_value"] or "null"),
                    "remote": json.loads(r["remote_value"] or "null"),
                    "detected_at": r["detected_at"]})
    return out


def card(conn, card_id: str) -> dict | None:
    from trello import commission
    c = mirror.get(conn, "card", card_id)
    if c is None:
        return None
    busy = outbox.pending_objects(conn)
    me = (mapping.get_config()["member"] or {}).get("id", "")
    lists = [{"id": r["id"], "name": r["name"]} for r in conn.execute(
        "SELECT id, name FROM trello_lists WHERE board_id = ? AND closed = 0 "
        "AND removed_at IS NULL ORDER BY pos", (c["board_id"],))]
    labels = [{"id": r["id"], "name": r["name"], "color": r["color"] or None}
              for r in conn.execute("SELECT * FROM trello_labels WHERE board_id = ? "
                                    "AND removed_at IS NULL ORDER BY name", (c["board_id"],))]
    checklists = []
    ids = {card_id}
    for cl in conn.execute("SELECT * FROM trello_checklists WHERE card_id = ? "
                           "AND removed_at IS NULL ORDER BY pos, id", (card_id,)):
        ids.add(cl["id"])
        items = []
        for it in conn.execute("SELECT * FROM trello_check_items WHERE checklist_id = ? "
                               "AND removed_at IS NULL ORDER BY pos, id", (cl["id"],)):
            ids.add(it["id"])
            items.append({"id": it["id"], "name": it["name"], "pos": it["pos"],
                          "state": it["state"],
                          "pending": ("checkitem", it["id"]) in busy})
        checklists.append({"id": cl["id"], "name": cl["name"], "pos": cl["pos"], "items": items})
    comments = []
    for cm in conn.execute("SELECT * FROM trello_comments WHERE card_id = ? "
                           "AND removed_at IS NULL ORDER BY date DESC", (card_id,)):
        ids.add(cm["id"])
        comments.append({"id": cm["id"], "author_name": cm["author_name"], "text": cm["text"],
                         "date": cm["date"],
                         "is_mine": bool(me) and cm["author_id"] == me or mirror.is_tmp(cm["id"]),
                         "pending": ("comment", cm["id"]) in busy or mirror.is_tmp(cm["id"])})
    comm = commission.card_commission(conn, card_id)
    return {
        "card": {"id": c["id"], "board_id": c["board_id"], "list_id": c["list_id"],
                 "name": c["name"], "desc": c["desc"], "pos": c["pos"],
                 "closed": bool(c["closed"]), "due": c["due"], "start": c["start"],
                 "due_complete": bool(c["due_complete"]), "labels": c["labels"],
                 "cover": c["cover"] or None, "cover_url": _cover_url(conn, c["cover"]),
                 "url": c["url"] or "", "removed": bool(c["removed_at"]),
                 "pending": ("card", card_id) in busy or mirror.is_tmp(card_id)},
        "lists": lists, "labels": labels, "checklists": checklists, "comments": comments,
        "commission": ({"id": comm["id"], "client_name": comm["client_name"],
                        "price": comm["price"], "currency": comm["currency"],
                        "status": comm["status"]} if comm else None),
        "conflicts": conflicts(conn, object_ids=ids),
    }
