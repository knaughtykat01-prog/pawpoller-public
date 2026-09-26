"""Commissions on the board (spec 006, Q1 → option A; research R10).

A card is a commission when a ``trello_links`` row names it — the same table spec
005 used, keyed on the commission's natural key ``(client_name, created_at)``
because ``commissions.id`` is a per-instance accident (005 research R1).

Which side owns what:

* **The card** owns status (through the list → status map), due date and
  archived. They are read from the card after every board read.
* **PawPoller** owns client, price, currency, description, notes, linked artwork
  and delivery sites. None of them is written to Trello (FR-030).

A status change or archive made on the Commissions page moves or archives the
card through the outbox, so both pages always describe the same thing.
"""
from __future__ import annotations

from database import commissions_queries as cq
from database import trello_queries as tq
from trello import card_block, mapping, mirror, outbox, positions


def _date(value) -> str:
    return str(value or "").split("T")[0][:10]


def card_commission(conn, card_id: str) -> dict | None:
    link = tq.get_link_by_card(conn, card_id)
    if not link:
        return None
    return find(conn, link["client_name"], link["created_at"])


def find(conn, client_name: str, created_at) -> dict | None:
    r = conn.execute("SELECT * FROM commissions WHERE client_name = ? AND created_at IS ?",
                     (client_name, created_at)).fetchone()
    return cq.get_commission(conn, r["id"]) if r else None


def mark(conn, card_id: str, *, client_name: str = "", price=0, currency: str = "USD") -> dict:
    card = mirror.get(conn, "card", card_id)
    if card is None:
        raise LookupError("No such card.")
    if tq.get_link_by_card(conn, card_id):
        raise ValueError("That card is already a commission.")
    status = mapping.status_for_list(card["list_id"]) or "quote"
    cid = cq.create_commission(conn, client_name=(client_name or card["name"])[:200],
                               price=price or 0, currency=currency or "USD", status=status,
                               due_date=_date(card.get("due")))
    conn.commit()
    c = cq.get_commission(conn, cid)
    tq.create_link(conn, client_name=c["client_name"], created_at=c["created_at"],
                   card_id=card_id, board_id=card["board_id"], card_url=card.get("url", ""))
    return c


def unmark(conn, card_id: str) -> bool:
    """Forget that the card is a commission. The commission row is KEPT."""
    link = tq.get_link_by_card(conn, card_id)
    if not link:
        return False
    tq.delete_link(conn, link["client_name"], link["created_at"])
    return True


def sync_from_board(conn, board_id: str) -> int:
    """Carry each linked card's status, due date and archived state onto its
    commission. A card deleted in Trello unlinks its commission and keeps it."""
    changed = 0
    for link in tq.list_links(conn):
        card = mirror.get(conn, "card", link["card_id"])
        if card is None or card["board_id"] != board_id:
            continue
        c = find(conn, link["client_name"], link["created_at"])
        if c is None:
            continue
        if card.get("removed_at"):
            tq.delete_link(conn, link["client_name"], link["created_at"])
            continue
        want = {"due_date": _date(card.get("due")),
                "archived": 1 if card.get("closed") else 0}
        status = mapping.status_for_list(card["list_id"])
        if status:
            want["status"] = status
        diff = {k: v for k, v in want.items() if str(c.get(k) or "") != str(v or "")}
        if diff:
            cq.update_commission(conn, c["id"], **diff)
            changed += 1
    conn.commit()
    return changed


def push_change(conn, commission: dict, *, status: str | None = None,
                archived: bool | None = None) -> bool:
    """A status or archive change made on the Commissions page → the card.

    Moves the card to the bottom of the first list mapped to ``status`` (by board
    order). Returns whether anything was queued.
    """
    link = tq.get_link(conn, commission["client_name"], commission["created_at"])
    card = mirror.get(conn, "card", link["card_id"]) if link else None
    if card is None or card.get("removed_at"):
        return False
    queued = False
    if status and mapping.status_for_list(card["list_id"]) != status:
        targets = mapping.lists_for_status(status)
        rows = [r for r in conn.execute(
            "SELECT id FROM trello_lists WHERE board_id = ? AND closed = 0 AND removed_at IS NULL "
            "ORDER BY pos", (card["board_id"],)) if r["id"] in targets]
        if rows:
            dest = rows[0]["id"]
            last = conn.execute("SELECT MAX(pos) FROM trello_cards WHERE list_id = ? "
                                "AND removed_at IS NULL", (dest,)).fetchone()[0]
            pos = positions.between(last, None)
            mirror.set_local(conn, "card", card["id"], {"list_id": dest, "pos": pos})
            outbox.enqueue(conn, "card.move", "card", card["id"], {"list_id": dest, "pos": pos})
            queued = True
    if archived is not None and bool(card.get("closed")) != bool(archived):
        mirror.set_local(conn, "card", card["id"], {"closed": 1 if archived else 0})
        outbox.enqueue(conn, "card.update", "card", card["id"], {"closed": bool(archived)})
        queued = True
    conn.commit()
    return queued


# ── spec 005 → 006 (FR-032) ──────────────────────────────────────────────────

def migration_plan(conn) -> dict:
    """What the one-time upgrade would do. Writes nothing."""
    cfg = mapping.get_config()
    list_status = dict(cfg["list_status"])
    for status, lid in (cfg.get("list_map") or {}).items():
        if lid and status in cq.STATUSES:
            list_status.setdefault(lid, status)
    strip = []
    for link in tq.list_links(conn):
        card = mirror.get(conn, "card", link["card_id"])
        if card and card_block.parse(card.get("desc", "")):
            strip.append({"card_id": card["id"], "name": card["name"]})
    linked = {(l["client_name"], l["created_at"]) for l in tq.list_links(conn)}
    create = [{"client_name": c["client_name"], "status": c["status"],
               "created_at": c["created_at"]}
              for c in cq.list_commissions(conn, archived=False)
              if (c["client_name"], c["created_at"]) not in linked]
    board = cfg["commission_board_id"] or cfg.get("board_id") or ""
    return {"board_id": board, "list_status": list_status, "strip": strip, "create": create}


def migrate(conn) -> dict:
    """Apply the plan: invert the mapping, tidy card descriptions, give every
    card-less active commission a card. Every Trello write goes through the
    outbox, so it is ordered, retried and visible like any other edit."""
    plan = migration_plan(conn)
    board = plan["board_id"]
    mapping.save_config(commission_board_id=board, list_status=plan["list_status"],
                        migrated_005=True)
    for s in plan["strip"]:
        card = mirror.get(conn, "card", s["card_id"])
        clean = card_block.strip(card.get("desc", ""))
        mirror.set_local(conn, "card", card["id"], {"desc": clean})
        outbox.enqueue(conn, "card.update", "card", card["id"], {"desc": clean})
    created = 0
    for item in plan["create"]:
        c = find(conn, item["client_name"], item["created_at"])
        targets = mapping.lists_for_status(c["status"]) if c else []
        dest = next((r["id"] for r in conn.execute(
            "SELECT id FROM trello_lists WHERE board_id = ? AND closed = 0 AND removed_at IS NULL "
            "ORDER BY pos", (board,)) if r["id"] in targets), "")
        if not dest:
            continue
        cid = mirror.tmp_id()
        last = conn.execute("SELECT MAX(pos) FROM trello_cards WHERE list_id = ?",
                            (dest,)).fetchone()[0]
        pos = positions.between(last, None)
        name = c["client_name"] + (f" — {c['description']}" if c.get("description") else "")
        mirror.insert_local(conn, "card", cid, {"board_id": board, "list_id": dest,
                                                "name": name[:500], "pos": pos})
        outbox.enqueue(conn, "card.create", "card", cid, {"name": name[:500], "pos": pos,
                                                          "list_id": dest})
        tq.create_link(conn, client_name=c["client_name"], created_at=c["created_at"],
                       card_id=cid, board_id=board)
        created += 1
    conn.commit()
    return {"ok": True, "stripped": len(plan["strip"]), "created": created}
