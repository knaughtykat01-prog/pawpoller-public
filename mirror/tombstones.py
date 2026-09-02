"""Delete tracking for the upward sync — mirroring Stage 3.

§D5 of the spec: the existing channel *cannot express a deletion*.
``save_settings`` is ``current.update(data)`` — a merge — so removing something
locally and syncing does not remove it upstream; the next pull resurrects it.
Additive-only is right for accounts and wrong for the four tables where a
delete is the user's actual intent: un-ignoring a submission, un-handling an
inbox comment, taking a piece out of a Masterpiece, taking an item out of a
Collection.

## Why triggers, when the spec rejects trigger journals

§D1 rejects "hand-rolled trigger journals" as an approach to the *whole*
problem — 98 tables of permanently maintained triggers, plus tombstone GC,
resurrection bugs and schema-migration coupling. That objection is about scale,
and it stands. This is four tables.

At four tables the trade runs the other way, because the alternative is
recording the tombstone at every call site that deletes. There are more of
those than there are tables, they are spread across query modules and routes,
raw SQL bypasses all of them, and ``ON DELETE CASCADE`` on
``collection_members`` bypasses them by construction — deleting a collection
removes its members without any Python running at all. A trigger sees every one
of those paths because it lives underneath them.

## Resurrection

A tombstone that outlives a re-insert is a bug with teeth: ignore a submission,
un-ignore it, ignore it again, and a stale tombstone would delete it upstream
after the push had already re-added it. So each table gets an INSERT trigger
that clears any tombstone for the same natural key. Delete-then-re-add is
therefore a no-op rather than a race, and the ordering inside one push does not
matter.

## Lifetime

Tombstones are this install's *outbox*. They are cleared only after the far
side acknowledges the specific key (``clear``), so a failed push retries rather
than dropping the delete — the same at-least-once shape as Stage 2's result
sweep. The table is classed LOC in the registry: sending it as data would
replay one install's outbox as the other's.

⚠ A Stage 1 pull replaces the desktop database wholesale, which discards any
tombstone that has not been delivered. That is why the desktop pushes before it
pulls (``routes/mirror_api.py``), and why the ordering is enforced in code
rather than left to whoever clicks the buttons.
"""
from __future__ import annotations

import logging
import sqlite3

logger = logging.getLogger(__name__)

# Natural-key parts are joined with ASCII 31 (unit separator) rather than JSON:
# the triggers have to build the same string in SQL, and `char(31)` works on
# every SQLite build this app might meet, whereas json_array() is only compiled
# in by default from 3.38. No platform code, submission id, comment id,
# Masterpiece name or member ref contains a unit separator.
KEY_SEP = "\x1f"

# (table, key columns) — the four tables §D5 names. The key columns must match
# what mirror/shr.py exports as that table's natural key, or a delete will be
# recorded under a name the far side cannot match.
_TOMBSTONED: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("ignored_submissions", ("platform", "submission_id")),
    ("inbox_state", ("platform", "comment_id")),
    ("masterpiece_members", ("masterpiece_name", "platform", "submission_id")),
    # collection_members' local key starts with the surrogate collection_id; the
    # trigger records that, and export_tombstones() resolves it to the
    # collection's name before it leaves the install. A trigger cannot do the
    # join itself without making the delete depend on the parent row still
    # existing — which, under ON DELETE CASCADE, it does not.
    ("collection_members", ("collection_id", "member_type", "member_ref")),
)


def encode_key(parts) -> str:
    return KEY_SEP.join("" if p is None else str(p) for p in parts)


def decode_key(key: str) -> list[str]:
    return key.split(KEY_SEP)


def _sql_key_expr(prefix: str, columns: tuple[str, ...]) -> str:
    """The SQL that builds the same string :func:`encode_key` builds.

    ``CAST(... AS TEXT)`` matters: ``submission_id`` is INTEGER on some tables
    and TEXT on others, and SQLite's ``||`` would otherwise render 12345 and
    '12345' identically only by luck of affinity.
    """
    return " || char(31) || ".join(
        f"CAST(COALESCE({prefix}.{c}, '') AS TEXT)" for c in columns)


def ensure_tombstones(conn: sqlite3.Connection) -> None:
    """Create the outbox and its triggers. Idempotent; safe on every startup.

    Triggers are created only for tables that exist — the platform and feature
    tables are created on demand, and a trigger on a missing table is an error
    rather than a no-op.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS mirror_tombstones (
            table_name  TEXT NOT NULL,
            natural_key TEXT NOT NULL,
            deleted_at  TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (table_name, natural_key)
        )
    """)

    live = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}

    for table, columns in _TOMBSTONED:
        if table not in live:
            continue
        old = _sql_key_expr("OLD", columns)
        new = _sql_key_expr("NEW", columns)
        conn.execute(f"""
            CREATE TRIGGER IF NOT EXISTS trg_mirror_tomb_{table}
            AFTER DELETE ON {table} BEGIN
                INSERT OR REPLACE INTO mirror_tombstones
                    (table_name, natural_key, deleted_at)
                VALUES ('{table}', {old}, datetime('now'));
            END
        """)
        # The resurrection guard. Without it, delete → re-add → push deletes
        # upstream the row the same push just re-created.
        conn.execute(f"""
            CREATE TRIGGER IF NOT EXISTS trg_mirror_undelete_{table}
            AFTER INSERT ON {table} BEGIN
                DELETE FROM mirror_tombstones
                 WHERE table_name = '{table}' AND natural_key = {new};
            END
        """)


def record(conn: sqlite3.Connection, table: str, parts) -> None:
    """Record a delete by hand.

    The triggers cover the four tables that have them; this exists for a caller
    that deletes from a table whose trigger has not been created yet (a fresh
    database mid-migration) and for the tests.
    """
    conn.execute(
        "INSERT OR REPLACE INTO mirror_tombstones (table_name, natural_key, deleted_at) "
        "VALUES (?, ?, datetime('now'))",
        (table, encode_key(parts)),
    )


def pending(conn: sqlite3.Connection, table: str | None = None) -> list[dict]:
    """Every undelivered delete, oldest first."""
    if not _has_table(conn):
        return []
    sql = "SELECT table_name, natural_key, deleted_at FROM mirror_tombstones"
    params: tuple = ()
    if table:
        sql += " WHERE table_name = ?"
        params = (table,)
    sql += " ORDER BY deleted_at ASC"
    return [{"table": r[0], "key": decode_key(r[1]), "deleted_at": r[2]}
            for r in conn.execute(sql, params).fetchall()]


def count(conn: sqlite3.Connection) -> int:
    if not _has_table(conn):
        return 0
    return conn.execute("SELECT COUNT(*) FROM mirror_tombstones").fetchone()[0]


def clear(conn: sqlite3.Connection, delivered, *, commit: bool = True) -> int:
    """Drop the tombstones the far side acknowledged.

    Takes the acknowledged keys rather than clearing everything, so a partial
    apply leaves the rest queued instead of silently dropping them.

    ``commit=False`` is for the receiving side, which calls this from inside
    ``shr.apply_bundle``'s single transaction — committing there would break the
    guarantee that a bundle lands whole or not at all.
    """
    if not _has_table(conn):
        return 0
    n = 0
    for item in delivered or []:
        table = item.get("table")
        key = item.get("key")
        if not table or key is None:
            continue
        encoded = key if isinstance(key, str) else encode_key(key)
        cur = conn.execute(
            "DELETE FROM mirror_tombstones WHERE table_name = ? AND natural_key = ?",
            (table, encoded),
        )
        n += cur.rowcount
    if commit:
        conn.commit()
    return n


def _has_table(conn: sqlite3.Connection) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='mirror_tombstones'"
    ).fetchone() is not None
