"""The board mirror: apply what Trello says to the local copy (spec 006).

Research R1, R2, R5. One rule, per object and per field, against the ``agreed``
value both sides last agreed on:

=======================  ============================  ==========================
remote vs agreed         a local op pending on field?  result
=======================  ============================  ==========================
same                     —                             nothing (local op will send)
differs                  no                            take remote
differs, = local         yes                           agreed catches up
differs, ≠ local         yes                           CONFLICT: hold op, keep both
=======================  ============================  ==========================

Two fields never conflict (R5): ``pos`` (order is not content — the local drag
wins and is sent), and ``labels``, which is a SET and merges as add/remove deltas
so two different labels added on two sides both survive.

⚠ Only a **successful** read can mark anything removed. Callers pass a read that
parsed; a failed read raises before reaching this module. A read that would mark
a large share of a board's cards removed stops instead (FR-026) — that is what a
wrong board or a half-answered read looks like, not what a person does.

Everything here takes a connection and does its own SQL, but no network: the
poller fetches, this module decides. `tests/test_trello_mirror.py` drives it with
plain dictionaries.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

# object type → (table, synced fields, scope column)
TYPES: dict[str, tuple[str, tuple, str]] = {
    "board": ("trello_boards", ("name", "closed", "url", "bg_color", "bg_image_url"), ""),
    "list": ("trello_lists", ("name", "pos", "closed"), "board_id"),
    "card": ("trello_cards", ("list_id", "name", "desc", "pos", "closed", "start", "due",
                              "due_complete", "labels", "cover", "url", "comment_count"),
             "board_id"),
    "label": ("trello_labels", ("name", "color"), "board_id"),
    "checklist": ("trello_checklists", ("card_id", "name", "pos"), "board_id"),
    "checkitem": ("trello_check_items", ("checklist_id", "name", "pos", "state"), "board_id"),
    "comment": ("trello_comments", ("card_id", "author_id", "author_name", "text", "date"),
                "board_id"),
}
JSON_FIELDS = {"labels", "cover"}
NEVER_CONFLICT = {"pos", "labels"}

# FR-026: this many cards AND this share of a board's live cards vanishing in one
# read stops the apply. Crude on purpose — it catches the systemic case.
REMOVE_ASK_MIN = 5
REMOVE_ASK_FRACTION = 0.5


class MassRemoval(Exception):
    """A read would mark too much of a board removed. Nothing was written."""


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def tmp_id() -> str:
    return f"tmp_{uuid.uuid4().hex}"


def is_tmp(object_id: str) -> bool:
    return str(object_id or "").startswith("tmp_")


# ── value handling ───────────────────────────────────────────────────────────

def _enc(field: str, value):
    """Python value → column value."""
    if field in JSON_FIELDS:
        return json.dumps(value if value is not None else ({} if field == "cover" else []),
                          sort_keys=True)
    if isinstance(value, bool):
        return 1 if value else 0
    return value


def _dec(field: str, value):
    """Column value → Python value."""
    if field in JSON_FIELDS:
        try:
            return json.loads(value) if isinstance(value, str) and value else (
                value if value is not None else ({} if field == "cover" else []))
        except (TypeError, ValueError):
            return {} if field == "cover" else []
    return value


def same(a, b) -> bool:
    """Compare two field values the way they are stored."""
    if isinstance(a, (list, dict)) or isinstance(b, (list, dict)):
        return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
    if isinstance(a, float) or isinstance(b, float):
        try:
            return abs(float(a or 0) - float(b or 0)) < 1e-9
        except (TypeError, ValueError):
            return False
    if isinstance(a, bool) or isinstance(b, bool) or (
            isinstance(a, int) and isinstance(b, int)):
        return int(bool(a)) == int(bool(b))
    return (a if a not in (None, "") else "") == (b if b not in (None, "") else "")


def row_dict(otype: str, row) -> dict:
    """A table row as a dict with JSON fields decoded and ``agreed`` parsed."""
    d = dict(row)
    for f in JSON_FIELDS:
        if f in d:
            d[f] = _dec(f, d[f])
    try:
        d["agreed"] = json.loads(d.get("agreed") or "{}")
    except (TypeError, ValueError):
        d["agreed"] = {}
    return d


def get(conn, otype: str, object_id: str) -> dict | None:
    table = TYPES[otype][0]
    r = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (object_id,)).fetchone()
    return row_dict(otype, r) if r else None


# ── normalising a Trello read ────────────────────────────────────────────────

def _pos(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def normalise_board_meta(b: dict) -> dict:
    prefs = b.get("prefs") or {}
    return {"id": b.get("id", ""), "name": b.get("name", "") or "",
            "closed": int(bool(b.get("closed"))), "url": b.get("url", "") or "",
            "bg_color": prefs.get("backgroundColor") or "",
            "bg_image_url": prefs.get("backgroundImage") or "",
            "last_activity": b.get("dateLastActivity") or ""}


def _cover(c: dict) -> dict:
    cov = c.get("cover") or {}
    out = {}
    if cov.get("color"):
        out["color"] = cov["color"]
    if cov.get("idAttachment"):
        out["attachment_id"] = cov["idAttachment"]
    if out:
        out["size"] = cov.get("size") or "normal"
        if cov.get("brightness"):
            out["brightness"] = cov["brightness"]
    return out


def normalise_board(read: dict) -> dict:
    """``{type: {id: row}}`` plus ``covers`` — every object the read carries."""
    bid = read["id"]
    out: dict = {t: {} for t in ("list", "card", "label", "checklist", "checkitem")}
    out["covers"] = {}
    for l in read.get("lists") or []:
        out["list"][l["id"]] = {"id": l["id"], "board_id": bid, "name": l.get("name", "") or "",
                                "pos": _pos(l.get("pos")), "closed": int(bool(l.get("closed")))}
    for c in read.get("cards") or []:
        badges = c.get("badges") or {}
        out["card"][c["id"]] = {
            "id": c["id"], "board_id": bid, "list_id": c.get("idList", "") or "",
            "name": c.get("name", "") or "", "desc": c.get("desc", "") or "",
            "pos": _pos(c.get("pos")), "closed": int(bool(c.get("closed"))),
            "start": c.get("start") or None, "due": c.get("due") or None,
            "due_complete": int(bool(c.get("dueComplete"))),
            "labels": sorted(c.get("idLabels") or []), "cover": _cover(c),
            "url": c.get("shortUrl", "") or "",
            "comment_count": int(badges.get("comments") or 0),
            "last_activity": c.get("dateLastActivity") or ""}
        for a in c.get("attachments") or []:
            if a.get("id") and a["id"] == (c.get("cover") or {}).get("idAttachment"):
                out["covers"][a["id"]] = {
                    "attachment_id": a["id"], "card_id": c["id"], "url": a.get("url", ""),
                    "file_name": a.get("fileName") or a.get("name") or "",
                    "mime": a.get("mimeType") or ""}
    for lb in read.get("labels") or []:
        out["label"][lb["id"]] = {"id": lb["id"], "board_id": bid,
                                  "name": lb.get("name", "") or "", "color": lb.get("color") or ""}
    for cl in read.get("checklists") or []:
        out["checklist"][cl["id"]] = {"id": cl["id"], "board_id": bid,
                                      "card_id": cl.get("idCard", "") or "",
                                      "name": cl.get("name", "") or "", "pos": _pos(cl.get("pos"))}
        for it in cl.get("checkItems") or []:
            out["checkitem"][it["id"]] = {
                "id": it["id"], "board_id": bid, "checklist_id": cl["id"],
                "name": it.get("name", "") or "", "pos": _pos(it.get("pos")),
                "state": "complete" if it.get("state") == "complete" else "incomplete"}
    return out


def normalise_comments(board_id: str, actions: list[dict]) -> dict:
    out = {}
    for a in actions or []:
        data = a.get("data") or {}
        card = (data.get("card") or {}).get("id", "")
        if not a.get("id") or not card:
            continue
        who = a.get("memberCreator") or {}
        out[a["id"]] = {"id": a["id"], "board_id": board_id or (data.get("board") or {}).get("id", ""),
                        "card_id": card, "author_id": a.get("idMemberCreator", "") or "",
                        "author_name": who.get("fullName") or who.get("username") or "",
                        "text": data.get("text", "") or "", "date": a.get("date", "") or ""}
    return out


# ── pending local changes ────────────────────────────────────────────────────

def pending_fields(conn) -> dict:
    """``{(type, id): {field, …}}`` for every op not yet accepted by Trello.

    Failed ops are excluded on purpose: Trello refused them, so the remote value
    is the truth and should overwrite the local one.
    """
    out: dict = {}
    for r in conn.execute("SELECT object_type, object_id, fields FROM trello_outbox "
                          "WHERE state IN ('pending', 'held')"):
        try:
            fields = set(json.loads(r["fields"] or "{}"))
        except (TypeError, ValueError):
            fields = set()
        out.setdefault((r["object_type"], r["object_id"]), set()).update(fields)
    return out


def _hold(conn, otype: str, object_id: str, field: str) -> None:
    for r in conn.execute("SELECT seq, fields FROM trello_outbox WHERE object_type = ? "
                          "AND object_id = ? AND state = 'pending'", (otype, object_id)).fetchall():
        try:
            if field in json.loads(r["fields"] or "{}"):
                conn.execute("UPDATE trello_outbox SET state = 'held' WHERE seq = ?", (r["seq"],))
        except (TypeError, ValueError):
            continue


def _conflict(conn, otype, object_id, field, agreed, local, remote) -> None:
    conn.execute(
        "INSERT INTO trello_mirror_conflicts (object_type, object_id, field, agreed_value, "
        "local_value, remote_value) VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(object_type, object_id, field) DO UPDATE SET "
        "agreed_value = excluded.agreed_value, local_value = excluded.local_value, "
        "remote_value = excluded.remote_value, detected_at = datetime('now')",
        (otype, object_id, field, json.dumps(agreed), json.dumps(local), json.dumps(remote)))
    _hold(conn, otype, object_id, field)


# ── reconciling ──────────────────────────────────────────────────────────────

def _write(conn, otype: str, object_id: str, values: dict, agreed: dict | None = None,
           insert: bool = False, extra: dict | None = None) -> None:
    table = TYPES[otype][0]
    cols = {k: _enc(k, v) for k, v in values.items() if k != "id"}
    cols.update(extra or {})
    if agreed is not None:
        cols["agreed"] = json.dumps(agreed, sort_keys=True)
    if insert:
        names = ["id"] + list(cols)
        conn.execute(f"INSERT INTO {table} ({', '.join(names)}) VALUES "
                     f"({', '.join('?' * len(names))})", [object_id, *cols.values()])
    elif cols:
        conn.execute(f"UPDATE {table} SET {', '.join(f'{k} = ?' for k in cols)} WHERE id = ?",
                     [*cols.values(), object_id])


def reconcile(conn, otype: str, remote: dict, *, scope: str = "", pending: dict | None = None,
              allow_remove: bool = True, only_ids: set | None = None,
              stamp: str | None = None) -> dict:
    """Bring one object type in one scope in line with a successful read.

    ``scope`` is the board id (or "" for boards themselves). ``only_ids``
    narrows removal to a subset — a card's own comment read can only speak for
    that card's comments.
    """
    table, fields, scope_col = TYPES[otype]
    pending = pending if pending is not None else pending_fields(conn)
    stamp = stamp or now()
    stats = {"added": 0, "updated": 0, "removed": 0, "conflicts": 0}

    if scope_col:
        local_rows = conn.execute(f"SELECT * FROM {table} WHERE {scope_col} = ?", (scope,)).fetchall()
    else:
        local_rows = conn.execute(f"SELECT * FROM {table}").fetchall()
    local = {r["id"]: row_dict(otype, r) for r in local_rows}

    for oid, r in remote.items():
        row = local.get(oid)
        extra_cols = {k: v for k, v in r.items() if k not in fields and k != "id"}
        if row is None:
            # New here — or moved in from another board (same id, other scope).
            existing = get(conn, otype, oid)
            vals = {f: r.get(f) for f in fields}
            agreed = {f: vals[f] for f in fields}
            if existing is None:
                _write(conn, otype, oid, vals, agreed, insert=True,
                       extra={**{k: _enc(k, v) for k, v in extra_cols.items()},
                              "synced_at": stamp})
            else:
                _write(conn, otype, oid, vals, agreed,
                       extra={**extra_cols, "removed_at": None, "synced_at": stamp})
            stats["added"] += 1
            continue

        agreed = dict(row["agreed"])
        updates: dict = {}
        busy = pending.get((otype, oid), set())
        for f in fields:
            rv, lv = r.get(f), row.get(f)
            av = agreed.get(f, lv)
            if f in busy:
                if same(rv, av):
                    continue                       # our op will send; Trello unchanged
                if same(rv, lv):
                    agreed[f] = rv                 # Trello already says what we will
                    continue
                if f == "labels":
                    merged = sorted((set(rv or []) | (set(lv or []) - set(av or [])))
                                    - (set(av or []) - set(lv or [])))
                    if not same(merged, lv):
                        updates[f] = merged
                    continue
                if f in NEVER_CONFLICT:
                    continue                       # our drag wins and is sent
                _conflict(conn, otype, oid, f, av, lv, rv)
                stats["conflicts"] += 1
                continue
            if not same(rv, lv):
                updates[f] = rv
            agreed[f] = rv
        if row.get("removed_at"):
            extra_cols["removed_at"] = None
        if updates or extra_cols or agreed != row["agreed"]:
            _write(conn, otype, oid, updates, agreed,
                   extra={**{k: _enc(k, v) for k, v in extra_cols.items()},
                          "synced_at": stamp})
            if updates:
                stats["updated"] += 1

    if allow_remove:
        for oid, row in local.items():
            if oid in remote or is_tmp(oid) or row.get("removed_at"):
                continue
            if only_ids is not None and oid not in only_ids:
                continue
            conn.execute(f"UPDATE {table} SET removed_at = ? WHERE id = ?", (stamp, oid))
            stats["removed"] += 1
    return stats


def would_remove(conn, board_id: str, remote_cards: dict) -> tuple[int, int]:
    """(cards this read would mark removed, live cards on the board)."""
    live = [r["id"] for r in conn.execute(
        "SELECT id FROM trello_cards WHERE board_id = ? AND removed_at IS NULL", (board_id,))
        if not is_tmp(r["id"])]
    return sum(1 for i in live if i not in remote_cards), len(live)


def apply_board(conn, read: dict, comments: list[dict] | None = None, *,
                confirm_removals: bool = False) -> dict:
    """Apply one successful board read (and its new comments). One transaction.

    Raises ``MassRemoval`` (having written nothing) when the read would remove too
    much of the board.
    """
    board_id = read["id"]
    objs = normalise_board(read)
    gone, live = would_remove(conn, board_id, objs["card"])
    if (not confirm_removals and gone >= REMOVE_ASK_MIN
            and live and gone / live >= REMOVE_ASK_FRACTION):
        raise MassRemoval(
            f"{gone} of {live} cards on a board are missing from Trello's answer. "
            "That usually means a wrong or emptied board; nothing was marked removed.")

    stamp = now()
    pending = pending_fields(conn)
    stats: dict = {}
    for otype in ("list", "label", "card", "checklist", "checkitem"):
        stats[otype] = reconcile(conn, otype, objs[otype], scope=board_id,
                                 pending=pending, stamp=stamp)
    if comments:
        stats["comment"] = reconcile(conn, "comment", normalise_comments(board_id, comments),
                                     scope=board_id, pending=pending, allow_remove=False,
                                     stamp=stamp)
    for cov in objs["covers"].values():
        conn.execute(
            "INSERT INTO trello_covers (attachment_id, card_id, url, file_name, mime) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(attachment_id) DO UPDATE SET "
            "card_id = excluded.card_id, url = excluded.url",
            (cov["attachment_id"], cov["card_id"], cov["url"], cov["file_name"], cov["mime"]))
    meta = normalise_board_meta(read)
    stats["board"] = reconcile(conn, "board", {board_id: meta}, allow_remove=False,
                               pending=pending, stamp=stamp)
    conn.execute("UPDATE trello_boards SET imported_at = COALESCE(imported_at, ?), "
                 "last_activity = ? WHERE id = ?", (stamp, meta["last_activity"], board_id))
    return stats


def apply_card_comments(conn, board_id: str, card_id: str, actions: list[dict]) -> dict:
    """A card's full comment list — the only read that can see edits and deletions."""
    remote = normalise_comments(board_id, actions)
    local_ids = {r["id"] for r in conn.execute(
        "SELECT id FROM trello_comments WHERE card_id = ?", (card_id,))}
    return reconcile(conn, "comment", remote, scope=board_id, only_ids=local_ids | set(remote))


def apply_member_boards(conn, boards: list[dict], extra_ids: set | None = None) -> dict:
    """The board list itself. ``filter=all`` includes closed boards, so a board
    absent from a successful, non-empty answer was deleted (R2).

    ⚠ An EMPTY answer while boards are known is treated as suspect, not as
    "every board was deleted" — nothing is removed.
    """
    remote = {}
    for b in boards or []:
        meta = normalise_board_meta(b)
        remote[meta["id"]] = meta
    known = conn.execute("SELECT COUNT(*) FROM trello_boards WHERE removed_at IS NULL").fetchone()[0]
    keep = set(extra_ids or ())
    local_ids = {r["id"] for r in conn.execute("SELECT id FROM trello_boards")}
    stats = reconcile(conn, "board", {k: {f: v for f, v in m.items() if f != "last_activity"}
                                      for k, m in remote.items()},
                      allow_remove=bool(remote) or not known,
                      only_ids=local_ids - keep)
    return {"stats": stats, "activity": {k: m["last_activity"] for k, m in remote.items()}}


# ── local edits ──────────────────────────────────────────────────────────────

def set_local(conn, otype: str, object_id: str, values: dict) -> None:
    """Write PawPoller's side of an edit. ``agreed`` is untouched — the outbox
    advances it when Trello accepts."""
    _write(conn, otype, object_id, values)


def insert_local(conn, otype: str, object_id: str, values: dict) -> None:
    """A new object made in PawPoller. ``agreed`` is empty: nothing agreed yet."""
    _write(conn, otype, object_id, values, agreed={}, insert=True)


def mark_removed(conn, otype: str, object_id: str) -> None:
    conn.execute(f"UPDATE {TYPES[otype][0]} SET removed_at = ? WHERE id = ?",
                 (now(), object_id))


def advance_agreed(conn, otype: str, object_id: str, values: dict) -> None:
    row = get(conn, otype, object_id)
    if not row:
        return
    agreed = dict(row["agreed"])
    agreed.update(values)
    conn.execute(f"UPDATE {TYPES[otype][0]} SET agreed = ?, synced_at = ? WHERE id = ?",
                 (json.dumps(agreed, sort_keys=True), now(), object_id))


# Every column that can hold another object's id, for re-keying a temp id once
# Trello has confirmed a create (R4).
_REFS = (("trello_lists", "id"), ("trello_cards", "id"), ("trello_cards", "list_id"),
         ("trello_labels", "id"), ("trello_checklists", "id"), ("trello_checklists", "card_id"),
         ("trello_check_items", "id"), ("trello_check_items", "checklist_id"),
         ("trello_comments", "id"), ("trello_comments", "card_id"),
         ("trello_covers", "card_id"), ("trello_outbox", "object_id"),
         ("trello_mirror_conflicts", "object_id"), ("trello_links", "card_id"))


def rekey(conn, old: str, new: str) -> None:
    """Replace a ``tmp_…`` id with Trello's everywhere it is referenced.

    Temp ids are 36 random hex characters, so a plain string replace inside the
    JSON columns (a card's label set, an op's fields) cannot hit anything else.
    """
    if not is_tmp(old) or not new:
        return
    for table, col in _REFS:
        conn.execute(f"UPDATE {table} SET {col} = ? WHERE {col} = ?", (new, old))
    conn.execute("UPDATE trello_cards SET labels = REPLACE(labels, ?, ?) WHERE labels LIKE ?",
                 (old, new, f"%{old}%"))
    conn.execute("UPDATE trello_outbox SET fields = REPLACE(fields, ?, ?) WHERE fields LIKE ?",
                 (old, new, f"%{old}%"))
