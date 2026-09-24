"""Link and conflict storage for the Trello sync (spec 005).

⚠ **Every function here keys on ``(client_name, created_at)``, never on
``commissions.id``.** That is not a style preference. `commissions` is SHR in
`mirror/registry.py` with ``exclude=("id",)`` and ``key=("client_name",
"created_at")`` — `mirror/shr.py:_apply_commissions` matches incoming rows on that
pair and lets the receiving instance assign its own rowid. An id is therefore a
local accident, and a link keyed on one silently re-points at whatever holds that
number after the next mirror pull.

If a future change adds a `commission_id` parameter to anything in this module,
that is the bug, not a convenience.
"""
from __future__ import annotations

import json
import sqlite3


def _row(r: sqlite3.Row) -> dict:
    d = dict(r)
    if "baseline" in d:
        try:
            d["baseline"] = json.loads(d["baseline"] or "{}")
        except (ValueError, TypeError):
            # A corrupt baseline must not take the sync down. An empty one reads
            # as "we have never agreed on anything", which makes every field look
            # locally changed — noisy, but it pushes rather than loses.
            d["baseline"] = {}
    return d


# ── Links ────────────────────────────────────────────────────────────────────

def get_link(conn: sqlite3.Connection, client_name: str, created_at: str) -> dict | None:
    r = conn.execute(
        "SELECT * FROM trello_links WHERE client_name = ? AND created_at IS ?",
        (client_name or "", created_at)).fetchone()
    return _row(r) if r else None


def get_link_by_card(conn: sqlite3.Connection, card_id: str) -> dict | None:
    r = conn.execute("SELECT * FROM trello_links WHERE card_id = ?",
                     (card_id or "",)).fetchone()
    return _row(r) if r else None


def list_links(conn: sqlite3.Connection, board_id: str = "") -> list[dict]:
    if board_id:
        rows = conn.execute("SELECT * FROM trello_links WHERE board_id = ? ORDER BY id",
                            (board_id,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM trello_links ORDER BY id").fetchall()
    return [_row(r) for r in rows]


def create_link(conn: sqlite3.Connection, *, client_name: str, created_at: str,
                card_id: str, board_id: str, card_url: str = "",
                baseline: dict | None = None) -> dict:
    conn.execute(
        "INSERT OR REPLACE INTO trello_links "
        "(client_name, created_at, card_id, card_url, board_id, baseline) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (client_name or "", created_at, card_id, card_url, board_id,
         json.dumps(baseline or {})))
    conn.commit()
    return get_link(conn, client_name, created_at)


def set_baseline_field(conn: sqlite3.Connection, client_name: str, created_at: str,
                       field: str, value) -> None:
    """Advance ONE field's baseline.

    ⚠ Called only after the write that field required has succeeded. Advancing the
    whole baseline in one go after a partial run is how an interrupted sync loses
    a change: the unwritten field would then look agreed rather than pending.
    """
    link = get_link(conn, client_name, created_at)
    if not link:
        return
    base = dict(link["baseline"])
    base[field] = value
    conn.execute(
        "UPDATE trello_links SET baseline = ?, last_synced_at = datetime('now') "
        "WHERE client_name = ? AND created_at IS ?",
        (json.dumps(base), client_name or "", created_at))
    conn.commit()


def touch_link(conn: sqlite3.Connection, client_name: str, created_at: str,
               card_last_activity: str = "") -> None:
    conn.execute(
        "UPDATE trello_links SET last_synced_at = datetime('now'), "
        "card_last_activity = ? WHERE client_name = ? AND created_at IS ?",
        (card_last_activity or "", client_name or "", created_at))
    conn.commit()


def delete_link(conn: sqlite3.Connection, client_name: str, created_at: str) -> None:
    """Drop the link only. The commission is never touched here — see FR-014."""
    conn.execute("DELETE FROM trello_links WHERE client_name = ? AND created_at IS ?",
                 (client_name or "", created_at))
    conn.commit()


# ── Conflicts ────────────────────────────────────────────────────────────────

def open_conflict(conn: sqlite3.Connection, *, client_name: str, created_at: str,
                  field: str, baseline_value="", local_value="", remote_value="") -> None:
    """Record a disagreement. Re-detecting the same one updates it in place."""
    conn.execute(
        "INSERT INTO trello_conflicts "
        "(client_name, created_at, field, baseline_value, local_value, remote_value) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(client_name, created_at, field) DO UPDATE SET "
        "baseline_value = excluded.baseline_value, local_value = excluded.local_value, "
        "remote_value = excluded.remote_value, detected_at = datetime('now')",
        (client_name or "", created_at, field,
         "" if baseline_value is None else str(baseline_value),
         "" if local_value is None else str(local_value),
         "" if remote_value is None else str(remote_value)))
    conn.commit()


def list_conflicts(conn: sqlite3.Connection, client_name: str = "",
                   created_at: str = "") -> list[dict]:
    if client_name or created_at:
        rows = conn.execute(
            "SELECT * FROM trello_conflicts WHERE client_name = ? AND created_at IS ? "
            "ORDER BY field", (client_name or "", created_at)).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM trello_conflicts ORDER BY detected_at DESC, field").fetchall()
    return [dict(r) for r in rows]


def conflict_fields(conn: sqlite3.Connection, client_name: str, created_at: str) -> set:
    """Which fields are currently blocked for this commission.

    A conflict blocks its own field and nothing else (FR-017) — this set is how
    the sync skips exactly those and carries on with the rest.
    """
    rows = conn.execute(
        "SELECT field FROM trello_conflicts WHERE client_name = ? AND created_at IS ?",
        (client_name or "", created_at)).fetchall()
    return {r["field"] for r in rows}


def clear_conflict(conn: sqlite3.Connection, client_name: str, created_at: str,
                   field: str) -> None:
    conn.execute(
        "DELETE FROM trello_conflicts WHERE client_name = ? AND created_at IS ? "
        "AND field = ?", (client_name or "", created_at, field))
    conn.commit()


def count_conflicts(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM trello_conflicts").fetchone()[0]
