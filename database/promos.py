"""Saved promo cards (Promo Maker v2 release 2, 4.16.0 — docs/specs/promo_maker_v2.md §3).

A promo is a *spec* (the excerpt, its highlight / style ranges, size, background, font,
footer) plus the rendered PNG and, when a photo background was used, that photo. The spec
is what makes a card editable months later; the PNG is what gets posted. Both live under
``DATA_DIR/promos`` and the row remembers their file names — never a path from a request.

Schema lives here (guarded ``CREATE TABLE IF NOT EXISTS``) and is applied both by
``db._run_migrations`` and by ``ensure()`` from the routes, so a fresh test database and a
years-old install both have it. The index is created AFTER the table, in the same place
(the migration-order gotcha: never index a migration-added table from a ``*_schema.sql``).
"""
from __future__ import annotations

import sqlite3

TABLE_SQL = """
    CREATE TABLE IF NOT EXISTS promos (
        promo_id    INTEGER PRIMARY KEY AUTOINCREMENT,
        story_name  TEXT,
        title       TEXT NOT NULL DEFAULT '',
        spec_json   TEXT NOT NULL,
        width       INTEGER NOT NULL,
        height      INTEGER NOT NULL,
        png_file    TEXT NOT NULL DEFAULT '',
        bg_file     TEXT,
        created_at  TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
    )
"""
INDEX_SQL = "CREATE INDEX IF NOT EXISTS idx_promos_story ON promos(story_name)"

_COLS = ("promo_id", "story_name", "title", "spec_json", "width", "height",
         "png_file", "bg_file", "created_at", "updated_at")


def ensure(conn: sqlite3.Connection) -> None:
    conn.execute(TABLE_SQL)
    conn.execute(INDEX_SQL)


def _row(r) -> dict | None:
    return dict(zip(_COLS, r)) if r else None


def list_promos(conn: sqlite3.Connection, story_name: str | None = None) -> list[dict]:
    """Newest first. ``story_name`` narrows to one story's cards."""
    ensure(conn)
    sql = "SELECT " + ", ".join(_COLS) + " FROM promos"
    args: tuple = ()
    if story_name:
        sql += " WHERE story_name = ?"
        args = (story_name,)
    sql += " ORDER BY updated_at DESC, promo_id DESC"
    return [_row(r) for r in conn.execute(sql, args).fetchall()]


def get_promo(conn: sqlite3.Connection, promo_id: int) -> dict | None:
    ensure(conn)
    return _row(conn.execute("SELECT " + ", ".join(_COLS) + " FROM promos WHERE promo_id = ?",
                             (int(promo_id),)).fetchone())


def create_promo(conn: sqlite3.Connection, *, story_name: str | None, title: str,
                 spec_json: str, width: int, height: int) -> int:
    """Insert the row first (the id names the files), return the id. The caller
    writes the files and then calls ``set_files``."""
    ensure(conn)
    cur = conn.execute(
        "INSERT INTO promos (story_name, title, spec_json, width, height) VALUES (?, ?, ?, ?, ?)",
        (story_name or None, title or "", spec_json, int(width), int(height)))
    conn.commit()
    return int(cur.lastrowid)


def set_files(conn: sqlite3.Connection, promo_id: int, png_file: str, bg_file: str | None) -> None:
    conn.execute("UPDATE promos SET png_file = ?, bg_file = ? WHERE promo_id = ?",
                 (png_file, bg_file, int(promo_id)))
    conn.commit()


def update_promo(conn: sqlite3.Connection, promo_id: int, *, story_name: str | None, title: str,
                 spec_json: str, width: int, height: int, bg_file: str | None) -> None:
    conn.execute(
        "UPDATE promos SET story_name = ?, title = ?, spec_json = ?, width = ?, height = ?, bg_file = ?, "
        "updated_at = datetime('now') WHERE promo_id = ?",
        (story_name or None, title or "", spec_json, int(width), int(height), bg_file, int(promo_id)))
    conn.commit()


def delete_promo(conn: sqlite3.Connection, promo_id: int) -> bool:
    cur = conn.execute("DELETE FROM promos WHERE promo_id = ?", (int(promo_id),))
    conn.commit()
    return cur.rowcount > 0
