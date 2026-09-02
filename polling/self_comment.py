"""Own-account comment detection — "is this comment ours?" (2.192.0)

A comment authored by the posting account is not engagement. Before this module
it counted as a NEW comment nearly everywhere: desktop toasts, Telegram pushes,
``new_comments_found``, the Inbox "N to answer" badge, the recent-comments
activity feed, and — most visibly — the Top Fans leaderboard, where your own
account ranked as your own top fan.

**Rows are STORED and FLAGGED (``is_own``), never dropped.** Keeping them means
the Inbox can still show your own replies as thread context, the captured-count
delta check stays aligned with the platform's reported count (dropping rows would
make the poller re-fetch the same thread forever), and a wrong handle match is
reversible by re-running the backfill instead of being unrecoverable.

**The comparison is a FULL normalised-handle match.** The pre-2.192 check in
``inbox_capture`` compared only the local part::

    author.lower().lstrip("@").split("@")[0] == own.split("@")[0]

so a Mastodon commenter ``@sam@some.other.instance`` matched our own
``@sam@our.instance`` and a stranger's comment was silently marked as ours.
Never split the host off the *incoming* author. Mastodon's varying ``acct``
format is handled instead by widening OUR side of the comparison (see
``own_handles``), which cannot produce that false positive.

Not solvable here: ``milestone_comments`` (``polling/telegram.py``) reads the
platform's own ``comments_count``/``replies`` snapshot column, computed
server-side by FA/IB/Bluesky and already inclusive of your replies. No
ingestion-side flag can reach it, and subtracting a local count would drift and
eventually go negative. Documented as a known limitation.
"""
from __future__ import annotations

import logging
import sqlite3

logger = logging.getLogger(__name__)

# Where each comment-capable platform's own identity is stored. The persisted
# "<code>_own_handle" (written at login by remember_own_handle) is always tried
# first; these are the pre-existing canonical keys, used as fallback.
#
# Platforms absent from this map store only an integer comments_count — there
# are no per-comment rows to flag, so they need no entry.
_IDENTITY_KEYS: dict[str, list[str]] = {
    "ib":   ["username"],
    "fa":   ["fa_username"],
    "e621": ["e621_username"],
    "da":   ["da_target_user"],
    # bsky_identifier is the LOGIN field and Bluesky permits an email address
    # there, so it is only trusted when it cannot be an email (see own_handles).
    "bsky": ["bsky_identifier"],
    # Mastodon has no username/handle setting at all — mast_instance_url and
    # mast_access_token are the only credential fields. Identity comes purely
    # from the persisted handle.
    "mast": [],
}

# Which table/author-column holds per-comment rows for each platform.
_COMMENT_SOURCES: dict[str, tuple[str, str]] = {
    "ib":   ("comments", "username"),
    "fa":   ("fa_comments", "username"),
    "bsky": ("platform_comments", "author"),
    "mast": ("platform_comments", "author"),
    "e621": ("platform_comments", "author"),
    "da":   ("platform_comments", "author"),
}


def normalise_handle(value) -> str:
    """Casefold, trim, drop ONE leading '@'. Never splits on an interior '@'."""
    if not value:
        return ""
    return str(value).strip().lower().lstrip("@").strip()


def own_handle_key(platform: str) -> str:
    """Settings key holding the handle we resolved at login for *platform*."""
    return f"{platform}_own_handle"


def _write_key(conn, platform: str, field: str,
               account_id: int | None) -> str:
    """The settings key to WRITE *field* to for this account.

    Follows the house convention (``config.account_setting_key``): the default
    account uses the bare field, extra accounts are namespaced
    ``acct_<id>_<field>``. Whether an id IS the default needs the accounts
    table, hence *conn*.
    """
    import config

    if account_id is None:
        return field
    default_id = None
    try:
        from database import accounts as _accounts
        default_id = _accounts.get_default_account_id(conn, platform)
    except Exception:  # noqa: BLE001 — fall back to namespaced, still readable
        pass
    return config.account_setting_key(account_id, field,
                                      account_id == default_id)


def remember_own_handle(conn, platform: str, handle: str,
                        account_id: int | None = None) -> None:
    """Persist the handle a client resolved at login.

    Needed because two consumers run with no client instance and therefore no
    runtime identity: the read-side filters (``get_top_fans``, ``get_inbox``)
    and ``backfill_own_comments``. Bluesky and Mastodon in particular only know
    their handle after ``validate_session()``.

    A handle is not a secret, so this key stays OUT of
    ``config.CREDENTIAL_FIELDS`` and lives in plaintext settings. Writes only on
    change — save_settings is a read-merge-write and this runs every cycle.
    """
    import config

    handle = normalise_handle(handle)
    if not handle:
        return
    key = _write_key(conn, platform, own_handle_key(platform), account_id)
    try:
        if normalise_handle(config.get_settings().get(key, "")) == handle:
            return
        config.save_settings({key: handle})
        logger.info("Remembered own %s handle for self-comment filtering", platform)
    except Exception as e:  # noqa: BLE001 — never fail a poll over bookkeeping
        logger.warning("Could not persist own %s handle: %s", platform, e)


def own_handles(conn, platform: str,
                account_id: int | None = None) -> set[str]:
    """Every normalised form that means "this is us" on *platform*.

    Widening OUR side (rather than narrowing the incoming author) is what makes
    the full-string comparison safe:

    - Mastodon's ``acct`` is a bare local name for home-instance users and
      ``user@host`` for remote ones. Storing ``sam@our.instance`` alone would
      never match our own home-instance comments, which arrive as ``sam``. So
      the bare local part is added too — a match on it still means a
      home-instance user of that name, i.e. us. A remote
      ``sam@other.social`` matches neither entry.
    - Bluesky handles never contain '@' (``sam.bsky.social``), so an
      ``bsky_identifier`` containing one is an email login and is discarded
      rather than compared.
    """
    import config

    try:
        settings = config.get_settings()
    except Exception:  # noqa: BLE001
        return set()

    out: set[str] = set()

    def _add(raw, *, bsky_guard: bool = False) -> None:
        if bsky_guard and platform == "bsky" and "@" in str(raw or ""):
            return          # email login, not a handle
        h = normalise_handle(raw)
        if h:
            out.add(h)

    # Both the bare and the namespaced key are read for every field. Reading
    # both is deliberate tolerance: it costs nothing and means a handle still
    # resolves if an account's default-ness changed after the key was written.
    def _keys(field: str) -> list[str]:
        if account_id is None:
            return [field]
        return [field, f"acct_{account_id}_{field}"]

    for key in _keys(own_handle_key(platform)):
        _add(settings.get(key, ""))

    for field in _IDENTITY_KEYS.get(platform, []):
        for key in _keys(field):
            _add(settings.get(key, ""), bsky_guard=True)

    if platform == "mast":
        for h in list(out):
            if "@" in h:
                out.add(h.split("@", 1)[0])

    return {h for h in out if h}


def is_own_author(author, handles: set[str]) -> bool:
    """True when *author* is one of our own handles. Full-string match only."""
    if not handles:
        return False        # unknown identity → never guess; treat as a stranger
    return normalise_handle(author) in handles


def backfill_own_comments(conn: sqlite3.Connection) -> dict[str, int]:
    """Retro-flag self-comments already stored. Idempotent.

    Mirrors the ``backfill_credential_stamps`` pattern (2.170): deliberately NOT
    run from ``_run_migrations``, because at migration time the handles may not
    be known yet (Mastodon/Bluesky resolve theirs at login). Callers invoke it
    opportunistically once identity is available, and it is re-runnable after
    connecting a new account.

    Returns {platform: rows_flagged} for the surfaces that changed.
    """
    flagged: dict[str, int] = {}

    for platform, (table, col) in _COMMENT_SOURCES.items():
        handles = own_handles(conn, platform)
        if not handles:
            continue
        scoped = table == "platform_comments"
        try:
            if scoped:
                rows = conn.execute(
                    f"SELECT DISTINCT {col} FROM {table} WHERE platform = ?",
                    (platform,)).fetchall()
            else:
                rows = conn.execute(
                    f"SELECT DISTINCT {col} FROM {table}").fetchall()
        except sqlite3.OperationalError:
            continue        # table missing on a legacy/partial DB

        mine = [r[0] for r in rows if is_own_author(r[0], handles)]
        if not mine:
            continue

        placeholders = ",".join("?" * len(mine))
        sql = (f"UPDATE {table} SET is_own = 1 "
               f"WHERE COALESCE(is_own, 0) = 0 AND {col} IN ({placeholders})")
        args: list = list(mine)
        if scoped:
            sql += " AND platform = ?"
            args.append(platform)
        try:
            cur = conn.execute(sql, args)
        except sqlite3.OperationalError:
            continue        # is_own column not migrated yet
        if cur.rowcount:
            flagged[platform] = flagged.get(platform, 0) + cur.rowcount

    if flagged:
        conn.commit()
        logger.info("Self-comment backfill flagged: %s", flagged)
    return flagged
