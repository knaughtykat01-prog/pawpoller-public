"""Persona registry — cross-platform account grouping (the identity layer above accounts).

A *persona* bundles one or more accounts (across any platforms) into a single
logical identity, so PawPoller can scope dashboards and segment notifications by
identity rather than by individual account. Each account belongs to at most one
persona via the nullable ``accounts.persona_id`` column (NULL = Unassigned).

The link is a **soft reference** (no SQL FOREIGN KEY): :func:`delete_persona` nulls
its accounts' ``persona_id`` here in the CRUD layer rather than relying on
``ON DELETE`` semantics — mirroring :func:`accounts.delete_account`'s leave-orphans
philosophy and keeping the FK-on connection simple. Personas ride the sync channel
via a ``_personas_manifest`` (mirroring ``_accounts_manifest``); which account
belongs to which persona syncs through the accounts manifest's ``persona_id`` field.
"""

from __future__ import annotations

import json
import logging
import sqlite3

from database import accounts as accounts_db

logger = logging.getLogger(__name__)

DEFAULT_COLOR = "#6c8cff"


def ensure_personas_table(conn: sqlite3.Connection) -> None:
    """Create the personas table if absent. Idempotent."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS personas (
            persona_id  INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL,
            color       TEXT NOT NULL DEFAULT '#6c8cff',
            sort_order  INTEGER NOT NULL DEFAULT 0,
            created_at  TEXT NOT NULL DEFAULT (datetime('now')),
            -- Per-persona posting defaults (gap-wave-3 §1). Existing installs
            -- gain these via guarded ALTERs in db.py _run_migrations.
            default_platforms   TEXT NOT NULL DEFAULT '',
            default_rating      TEXT NOT NULL DEFAULT '',
            preferred_post_time TEXT NOT NULL DEFAULT ''
        );
        """
    )


# ── CRUD ───────────────────────────────────────────────────────

def list_personas(conn: sqlite3.Connection) -> list[dict]:
    sql = "SELECT * FROM personas ORDER BY sort_order, persona_id"
    return [dict(r) for r in conn.execute(sql).fetchall()]


def get_persona(conn: sqlite3.Connection, persona_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM personas WHERE persona_id = ?", (persona_id,)).fetchone()
    return dict(row) if row else None


def _next_sort_order(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COALESCE(MAX(sort_order), -1) + 1 AS n FROM personas").fetchone()
    return row["n"] if row else 0


def create_persona(conn: sqlite3.Connection, name: str, color: str = DEFAULT_COLOR) -> int:
    cur = conn.execute(
        "INSERT INTO personas (name, color, sort_order) VALUES (?, ?, ?)",
        (name or "Persona", color or DEFAULT_COLOR, _next_sort_order(conn)),
    )
    conn.commit()
    return cur.lastrowid


def update_persona(conn: sqlite3.Connection, persona_id: int, **fields) -> bool:
    """Update name/color/sort_order on a persona. Returns True if a row changed."""
    allowed = {"name", "color", "sort_order",
               "default_platforms", "default_rating", "preferred_post_time"}
    sets, params = [], []
    for key, val in fields.items():
        if key not in allowed or val is None:
            continue
        sets.append(f"{key} = ?")
        params.append(val)
    if not sets:
        return False
    params.append(persona_id)
    cur = conn.execute(f"UPDATE personas SET {', '.join(sets)} WHERE persona_id = ?", params)
    conn.commit()
    return cur.rowcount > 0


def delete_persona(conn: sqlite3.Connection, persona_id: int) -> bool:
    """Delete a persona. Its accounts fall back to Unassigned (persona_id NULL)
    FIRST, so no account is left pointing at a missing persona."""
    conn.execute("UPDATE accounts SET persona_id = NULL WHERE persona_id = ?", (persona_id,))
    cur = conn.execute("DELETE FROM personas WHERE persona_id = ?", (persona_id,))
    conn.commit()
    return cur.rowcount > 0


def assign_account_persona(conn: sqlite3.Connection, account_id: int,
                           persona_id: int | None) -> None:
    """Set (or clear, when *persona_id* is None) an account's persona. Dedicated
    because :func:`accounts.update_account` skips ``None`` values and so cannot
    unassign."""
    conn.execute(
        "UPDATE accounts SET persona_id = ? WHERE account_id = ?",
        (persona_id, account_id),
    )
    conn.commit()


def list_accounts_by_persona(conn: sqlite3.Connection,
                             enabled_only: bool = False) -> dict:
    """Group all accounts by persona_id. The ``None`` key is the Unassigned bucket."""
    groups: dict = {}
    for a in accounts_db.list_accounts(conn, enabled_only=enabled_only):
        groups.setdefault(a.get("persona_id"), []).append(a)
    return groups


def persona_stats(conn: sqlite3.Connection, persona_id: int) -> dict:
    """Combined {submissions, views, favorites, comments} for a persona, summed
    across its accounts, plus a per-platform breakdown. Reuses
    :func:`accounts.account_stats` — no new SQL. Accounts on not-yet-account-aware
    platforms (account_stats → None) contribute nothing."""
    # `score` pools separately from views: the booru family's metric is a net
    # up−down total that can be negative, so it must never land in a view count.
    combined = {"submissions": 0, "views": 0, "favorites": 0, "comments": 0, "score": 0}
    by_platform: dict = {}
    for a in accounts_db.list_accounts(conn):
        if a.get("persona_id") != persona_id:
            continue
        st = accounts_db.account_stats(conn, a["account_id"], a["platform"])
        if not st:
            continue
        bp = by_platform.setdefault(
            a["platform"],
            {"submissions": 0, "views": 0, "favorites": 0, "comments": 0, "score": 0},
        )
        for k in combined:
            v = st.get(k, 0) or 0
            combined[k] += v
            bp[k] += v
    return {"combined": combined, "by_platform": by_platform}


# ── Sync manifest (desktop ↔ server persona parity) ────────────
# Mirrors accounts.get_manifest/apply_manifest. The persona ROWS travel here;
# which account belongs to which persona travels in the accounts manifest's
# persona_id field. Additive upsert (never deletes), preserves persona_id.

def get_manifest(conn: sqlite3.Connection) -> list[dict]:
    # r.keys() guard: a peer that predates the posting-defaults columns
    # (gap-wave-3) still produces a valid manifest.
    fields = ("persona_id", "name", "color", "sort_order",
              "default_platforms", "default_rating", "preferred_post_time")
    return [
        {k: r[k] for k in fields if k in r.keys()}
        for r in conn.execute("SELECT * FROM personas ORDER BY persona_id").fetchall()
    ]


def apply_manifest(conn: sqlite3.Connection, manifest) -> int:
    """Upsert personas from a sync manifest. Additive only (no deletes).
    Preserves persona_id so the surrogate key stays stable across desktop↔server."""
    if isinstance(manifest, str):
        try:
            manifest = json.loads(manifest)
        except (ValueError, TypeError):
            return 0
    if not isinstance(manifest, list):
        return 0
    n = 0
    for p in manifest:
        try:
            pid = int(p["persona_id"])
        except (KeyError, TypeError, ValueError):
            continue
        conn.execute(
            "INSERT INTO personas (persona_id, name, color, sort_order,"
            "   default_platforms, default_rating, preferred_post_time)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(persona_id) DO UPDATE SET"
            "   name=excluded.name, color=excluded.color, sort_order=excluded.sort_order,"
            "   default_platforms=excluded.default_platforms,"
            "   default_rating=excluded.default_rating,"
            "   preferred_post_time=excluded.preferred_post_time",
            (pid, p.get("name", "Persona"), p.get("color", DEFAULT_COLOR),
             int(p.get("sort_order", 0)),
             # Absent on manifests from pre-wave-3 peers → keep sensible blanks.
             p.get("default_platforms", ""), p.get("default_rating", ""),
             p.get("preferred_post_time", "")),
        )
        n += 1
    conn.commit()
    return n
