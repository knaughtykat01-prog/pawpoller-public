"""Beta-reader draft share tokens (gap-wave-5 §3).

A tokenized, read-only public link to preview a story draft — no login. The
token maps to a story folder name; the public ``/share/{token}`` route in
dashboard.py renders that story's styled HTML if the token is enabled and
unexpired.

One table, created idempotently from ``_run_migrations`` (the followers /
inbox pattern):

- ``share_tokens`` — ``share_token`` (PK, ``secrets.token_urlsafe``),
  ``story_name``, ``created_at``, ``expires_at`` (NULL = never), ``enabled``
  (0/1; revoke flips it rather than deleting, so a revoked link stays 404 and
  the row survives for the "active shares" list to distinguish from a typo).

Deliberately minimal — no hit counter, no per-reader tracking. A draft share
is a courtesy link, not an analytics surface.
"""
from __future__ import annotations

import secrets
import sqlite3
from datetime import datetime, timezone


def ensure_share_tokens_table(conn: sqlite3.Connection) -> None:
    """Create the share_tokens table if missing. Idempotent; called every startup."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS share_tokens (
            share_token   TEXT PRIMARY KEY,
            story_name    TEXT NOT NULL,
            created_at    TEXT NOT NULL DEFAULT (datetime('now')),
            expires_at    TEXT,
            enabled       INTEGER NOT NULL DEFAULT 1
        )""")
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_share_tokens_story
            ON share_tokens(story_name)""")
    conn.commit()


def create_token(
    conn: sqlite3.Connection,
    story_name: str,
    *,
    expires_at: str | None = None,
) -> dict:
    """Mint a new share token for a story. Returns the created row as a dict.

    ``expires_at`` is an ISO-8601 UTC string (or None for a link that never
    expires). Each call creates a distinct token — a story can have several
    live share links (e.g. one per beta reader).
    """
    token = secrets.token_urlsafe(24)
    conn.execute(
        "INSERT INTO share_tokens (share_token, story_name, expires_at, enabled) "
        "VALUES (?, ?, ?, 1)",
        (token, story_name, expires_at),
    )
    conn.commit()
    row = conn.execute(
        "SELECT * FROM share_tokens WHERE share_token = ?", (token,)
    ).fetchone()
    return dict(row)


def get_token(conn: sqlite3.Connection, token: str) -> dict | None:
    """Look up a token row. Returns None if unknown. Does NOT check expiry —
    callers use ``is_live`` for the enabled + not-expired gate."""
    row = conn.execute(
        "SELECT * FROM share_tokens WHERE share_token = ?", (token,)
    ).fetchone()
    return dict(row) if row else None


def is_live(row: dict | None) -> bool:
    """True if a token row is enabled and not past its expiry. The single
    gate the public route uses — mirrors it so tests can assert directly."""
    if not row or not row.get("enabled"):
        return False
    exp = row.get("expires_at")
    if not exp:
        return True
    try:
        # Stored ISO strings may or may not carry a timezone; treat naive as UTC.
        dt = datetime.fromisoformat(exp)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return True  # unparseable expiry → treat as non-expiring rather than lock out
    return datetime.now(timezone.utc) < dt


def list_active_tokens(conn: sqlite3.Connection, story_name: str) -> list[dict]:
    """All enabled tokens for a story, newest first. Expired-but-enabled rows
    are included (the UI flags them) so the owner can see and revoke them."""
    rows = conn.execute(
        "SELECT * FROM share_tokens WHERE story_name = ? AND enabled = 1 "
        "ORDER BY created_at DESC",
        (story_name,),
    ).fetchall()
    return [dict(r) for r in rows]


def revoke_token(conn: sqlite3.Connection, token: str) -> bool:
    """Disable a token (soft delete). Returns True if a row was affected."""
    cur = conn.execute(
        "UPDATE share_tokens SET enabled = 0 WHERE share_token = ? AND enabled = 1",
        (token,),
    )
    conn.commit()
    return cur.rowcount > 0
