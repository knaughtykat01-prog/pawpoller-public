"""Thin old stats snapshots to one a day (4.38.0).

Every poll writes a snapshot per submission, and they were kept for ever — the
database's only unbounded growth (X alone had 47k rows). Charts over months and
years only need a point a day, so rows older than ``KEEP_FULL_DAYS`` are thinned
to the LAST snapshot of each day per submission (per account too, where the table
has one). Recent history keeps every snapshot.

The operator chose one year of full detail (2026-09-27). ⚠ This permanently
deletes rows — it is the one place in PawPoller that does, and only thins, never
empties: every submission keeps at least one row per day it was ever polled.

Tables are found by their columns (``submission_id`` + ``polled_at``) rather than
by a list, so a platform added later is covered without anyone remembering to.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

KEEP_FULL_DAYS = 365


def snapshot_tables(conn) -> list[tuple[str, bool]]:
    """``[(table, has_account_id)]`` for every table shaped like a stats snapshot."""
    out = []
    for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' "
                                "AND name LIKE '%snapshots' ORDER BY name").fetchall():
        cols = {r[1] for r in conn.execute(f'PRAGMA table_info("{name}")')}
        if {"id", "submission_id", "polled_at"} <= cols:
            out.append((name, "account_id" in cols))
    return out


def prune(conn, keep_full_days: int = KEEP_FULL_DAYS) -> dict:
    """Thin every snapshot table. One commit per table, so no long write lock."""
    cutoff = conn.execute("SELECT datetime('now', ?)", (f"-{int(keep_full_days)} days",)).fetchone()[0]
    removed = {}
    for table, has_account in snapshot_tables(conn):
        key = "account_id, submission_id" if has_account else "submission_id"
        cur = conn.execute(
            f'DELETE FROM "{table}" WHERE polled_at < ? AND id NOT IN ('
            f'  SELECT MAX(id) FROM "{table}" WHERE polled_at < ? '
            f'  GROUP BY {key}, date(polled_at))', (cutoff, cutoff))
        conn.commit()
        if cur.rowcount:
            removed[table] = cur.rowcount
    if removed:
        logger.info("Snapshot thinning removed %d old rows.", sum(removed.values()))
    return removed
