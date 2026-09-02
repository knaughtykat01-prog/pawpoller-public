"""Ignored discovered submissions (2.140.0).

A small user "Ignore" list for discovered artwork tiles that should never appear
in the Artwork hub — e.g. images the pollers scraped from tweets/microblog posts
that aren't real gallery work. Keyed by (platform, submission_id); the
discovered-unlinked query subtracts these. Fully reversible (un-ignore = delete).
"""
from __future__ import annotations

import sqlite3


def add_ignored(conn: sqlite3.Connection, platform: str, submission_id) -> None:
    """Mark a discovered (platform, submission_id) as ignored. Idempotent."""
    conn.execute(
        "INSERT OR IGNORE INTO ignored_submissions (platform, submission_id) "
        "VALUES (?, ?)", (platform, str(submission_id)))
    conn.commit()


def remove_ignored(conn: sqlite3.Connection, platform: str, submission_id) -> None:
    """Un-ignore: the tile returns to the discovered list on the next load."""
    conn.execute(
        "DELETE FROM ignored_submissions WHERE platform = ? AND submission_id = ?",
        (platform, str(submission_id)))
    conn.commit()


def all_ignored_pairs(conn: sqlite3.Connection) -> set[tuple]:
    """Every ignored `(platform, submission_id)` — subtracted from discovered."""
    return {
        (r["platform"], str(r["submission_id"]))
        for r in conn.execute(
            "SELECT platform, submission_id FROM ignored_submissions")
    }


def list_ignored(conn: sqlite3.Connection) -> list[dict]:
    """Ignored rows (most-recent first) for a restore view."""
    return [
        {"platform": r["platform"], "submission_id": str(r["submission_id"]),
         "ignored_at": r["ignored_at"]}
        for r in conn.execute(
            "SELECT platform, submission_id, ignored_at FROM ignored_submissions "
            "ORDER BY ignored_at DESC")
    ]
