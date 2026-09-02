"""Cross-platform follower / watcher count tracking.

An account's follower count is a single integer that means the same thing on
every platform, so — unlike the per-platform *submission* tables whose columns
differ wildly — follower history lives in ONE shared table keyed by the global
``account_id``:

- ``account_follower_snapshots`` is the time-series (one row per poll cycle per
  account) that feeds the follower growth chart, exactly like the per-submission
  snapshot tables feed the views-over-time chart.
- The current value is also cached on ``accounts.follower_count`` (+ ``_at``
  timestamp) so the Accounts page can show every account's follower number
  without an N-subquery join.

Platforms whose API exposes a follower/watcher count for the polled account:
Weasyl, DeviantArt, Wattpad, Itaku, Bluesky, X/Twitter, Mastodon, Pixiv. AO3,
SquidgeWorld and Tumblr expose no reliable public count and are not recorded
here (Inkbunny/FA/SoFurry already track individual *watchers* in their own
tables — this module is the lightweight count-only layer for everyone else).
"""

from __future__ import annotations

import sqlite3

# Platform codes whose client can fetch a follower count for the polled account.
# Keep in sync with the FOLLOWER_FETCHERS registry in polling/followers.py.
FOLLOWER_PLATFORMS = {"ws", "da", "wp", "ik", "bsky", "tw", "mast", "pix", "fn"}


def ensure_follower_tables(conn: sqlite3.Connection) -> None:
    """Create the shared follower snapshot table + cache columns. Idempotent.

    Safe to call on every startup. The ``accounts`` cache columns are added via
    ALTER guarded on the "duplicate column" error so re-runs are no-ops.
    """
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS account_follower_snapshots (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id  INTEGER NOT NULL,
            polled_at   TEXT NOT NULL DEFAULT (datetime('now')),
            followers   INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_afs_account_polled
            ON account_follower_snapshots(account_id, polled_at);
        """
    )
    for col, ddl in (("follower_count", "INTEGER NOT NULL DEFAULT 0"),
                     ("follower_count_at", "TEXT")):
        try:
            conn.execute(f"ALTER TABLE accounts ADD COLUMN {col} {ddl}")
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e).lower():
                raise


def record_snapshot(conn: sqlite3.Connection, account_id: int | None,
                    followers: int | None) -> bool:
    """Append a follower snapshot for ``account_id`` and refresh its cached count.

    Returns True if a snapshot row was written. A ``None`` follower value (the
    fetch failed or the platform can't report it) is skipped entirely — we never
    write a bogus 0, which would corrupt the growth series the same way the
    2.27.1 zero-snapshot bug corrupted the views series.
    """
    if account_id is None or followers is None:
        return False
    try:
        followers = int(followers)
    except (TypeError, ValueError):
        return False
    if followers < 0:
        return False
    conn.execute(
        "INSERT INTO account_follower_snapshots (account_id, followers) VALUES (?, ?)",
        (account_id, followers),
    )
    conn.execute(
        "UPDATE accounts SET follower_count = ?, follower_count_at = datetime('now') "
        "WHERE account_id = ?",
        (followers, account_id),
    )
    return True


def latest_count(conn: sqlite3.Connection, account_id: int) -> dict | None:
    """Current cached follower count + timestamp for one account, or None."""
    row = conn.execute(
        "SELECT follower_count, follower_count_at FROM accounts WHERE account_id = ?",
        (account_id,),
    ).fetchone()
    if row is None:
        return None
    return {"followers": row[0], "at": row[1]}


def platform_latest(conn: sqlite3.Connection, platform: str,
                    account_id: int | None = None) -> dict | None:
    """Follower count for a platform's default (or a specific) account.

    Used by the platform dashboard summary — sums nothing, just returns the one
    account's cached count. When ``account_id`` is None the platform's default
    account is used so the aggregate dashboard shows *something* sensible.
    """
    if account_id is not None:
        return latest_count(conn, account_id)
    row = conn.execute(
        "SELECT follower_count, follower_count_at FROM accounts "
        "WHERE platform = ? ORDER BY is_default DESC, sort_order, account_id LIMIT 1",
        (platform,),
    ).fetchone()
    if row is None:
        return None
    return {"followers": row[0], "at": row[1]}


def get_series(conn: sqlite3.Connection, account_id: int,
               since: str | None = None) -> list[dict]:
    """Follower time-series for one account, oldest first. For the growth chart."""
    sql = ("SELECT polled_at, followers FROM account_follower_snapshots "
           "WHERE account_id = ?")
    params: list = [account_id]
    if since:
        sql += " AND polled_at >= ?"
        params.append(since)
    sql += " ORDER BY polled_at ASC"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def platform_series(conn: sqlite3.Connection, platform: str,
                    account_id: int | None = None,
                    since: str | None = None) -> list[dict]:
    """Follower series for a platform's default (or specific) account."""
    if account_id is None:
        from database import accounts as _accounts
        account_id = _accounts.get_default_account_id(conn, platform)
    if account_id is None:
        return []
    return get_series(conn, account_id, since=since)
