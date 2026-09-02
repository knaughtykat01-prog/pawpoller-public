"""Cross-platform account registry (multi-account support).

This module is the single source of truth for the *identity* layer that lets
PawPoller run more than one account on the same platform (e.g. two FurAffinity
accounts) simultaneously. Before multi-account, every platform had exactly one
implicit account whose credentials lived under flat keys in settings.json
(``username``/``password`` for Inkbunny, ``fa_username``/``fa_cookie_a`` for FA,
etc.). That implicit account is now modelled as the platform's **default
account** (``is_default=1``) and keeps using those legacy flat keys verbatim, so
existing installs migrate with zero credential movement. Additional accounts are
purely additive and store their credentials under ``acct_<id>_<field>`` keys
(see ``config.get_account_credentials`` / ``config.is_credential_key``).

The ``accounts`` table uses a single global surrogate key (``account_id``) shared
across all platforms — it is what threads through every per-platform analytics
and posting table as the account discriminator.

Design notes:
- ``account_id`` is AUTOINCREMENT and therefore NOT uniformly 1 per platform.
  Any backfill of existing data rows must target *that platform's* default
  ``account_id`` (resolve via :func:`get_default_account_id`), never a literal 1.
- A partial unique index enforces at most one ``is_default`` account per platform.
"""

from __future__ import annotations

import json
import logging
import sqlite3

from database import platform_metrics

logger = logging.getLogger(__name__)

# All platform codes PawPoller knows about. Order is the display order.
PLATFORMS = ["ib", "fa", "ws", "sf", "sqw", "ao3", "da", "wp", "ik", "bsky", "tw", "mast", "tum", "pix", "thr", "ig", "e621"]

PLATFORM_NAMES = {
    "ib": "Inkbunny", "fa": "FurAffinity", "ws": "Weasyl", "sf": "SoFurry",
    "sqw": "SquidgeWorld", "ao3": "AO3", "da": "DeviantArt", "wp": "Wattpad",
    "ik": "Itaku", "bsky": "Bluesky", "tw": "X/Twitter", "mast": "Mastodon",
    "tum": "Tumblr", "pix": "Pixiv", "thr": "Threads", "ig": "Instagram",
    "e621": "e621",
}

# Predicate per platform: does settings hold credentials for a default account?
# Mirrors the ``checks`` list in server.py ``_poll_all`` — keep the two in sync.
DEFAULT_CRED_CHECKS = {
    "ib": lambda s: bool(s.get("username") and s.get("password")),
    "fa": lambda s: bool(s.get("fa_username") and s.get("fa_cookie_a")),
    "ws": lambda s: bool(s.get("ws_api_key")),
    "sf": lambda s: bool(s.get("sf_api_token")),
    "sqw": lambda s: bool(s.get("sqw_username") and s.get("sqw_password")),
    "ao3": lambda s: bool((s.get("ao3_username") and s.get("ao3_password"))
                          or s.get("ao3_session_cookie")),
    # OAuth (client_id + client_secret) is the real path since 2.47.0; the
    # cookie is the legacy `_napi` fallback. This check still demanded the
    # cookie, so an OAuth-only install seeded NO DeviantArt account at all —
    # and without an account there is nothing to attach a per-account posting
    # token to. Mirrors the gate da_poller already applies.
    "da": lambda s: bool(s.get("da_target_user")
                         and ((s.get("da_client_id") and s.get("da_client_secret"))
                              or s.get("da_cookie"))),
    "wp": lambda s: bool(s.get("wp_target_user")),
    "ik": lambda s: bool(s.get("ik_target_user")),
    "bsky": lambda s: bool(s.get("bsky_identifier") and s.get("bsky_app_password")),
    "tw": lambda s: bool(s.get("tw_auth_token") and s.get("tw_target_user")),
    "mast": lambda s: bool(s.get("mast_instance_url") and s.get("mast_access_token")),
    "tum": lambda s: bool(s.get("tum_api_key") and s.get("tum_blog")),
    "pix": lambda s: bool(s.get("pix_refresh_token")),
    "thr": lambda s: bool(s.get("thr_access_token")),
    "ig": lambda s: bool(s.get("ig_access_token")),
    "e621": lambda s: bool(s.get("e621_username") and s.get("e621_api_key")),
}

# The flat settings key whose value names the default account (for display).
_HANDLE_KEYS = {
    "ib": ["username"],
    "fa": ["fa_username"],
    "ws": ["ws_username"],
    "sf": ["sf_display_name"],
    "sqw": ["sqw_author_username", "sqw_username"],
    "ao3": ["ao3_username"],
    "da": ["da_target_user"],
    "wp": ["wp_target_user"],
    "ik": ["ik_target_user"],
    "bsky": ["bsky_identifier"],
    "tw": ["tw_target_user"],
    "mast": ["mast_instance_url"],
    "tum": ["tum_blog"],
    "pix": ["pix_user_id"],
    "thr": ["thr_username", "thr_user_id"],
    "ig": ["ig_username", "ig_user_id"],
    "e621": ["e621_username"],
}


def ensure_accounts_table(conn: sqlite3.Connection) -> None:
    """Create the accounts table + indexes if absent. Idempotent."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS accounts (
            account_id  INTEGER PRIMARY KEY AUTOINCREMENT,
            platform    TEXT NOT NULL,
            label       TEXT NOT NULL DEFAULT '',
            handle      TEXT NOT NULL DEFAULT '',
            enabled     INTEGER NOT NULL DEFAULT 1,
            is_default  INTEGER NOT NULL DEFAULT 0,
            sort_order  INTEGER NOT NULL DEFAULT 0,
            created_at  TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_accounts_platform ON accounts(platform, enabled);
        -- At most one default account per platform.
        CREATE UNIQUE INDEX IF NOT EXISTS idx_accounts_one_default
            ON accounts(platform) WHERE is_default = 1;
        """
    )


def _default_handle(platform: str, settings: dict) -> str:
    for key in _HANDLE_KEYS.get(platform, []):
        val = settings.get(key)
        if val:
            return str(val)
    return ""


def derive_handle(platform: str, source: dict) -> str:
    """Best-effort display handle from a creds/settings-shaped dict."""
    return _default_handle(platform, source)


def get_default_account_id(conn: sqlite3.Connection, platform: str,
                           create: bool = False, settings: dict | None = None) -> int | None:
    """Return the default account_id for *platform*.

    When *create* is True and no default exists yet, one is created on the spot
    (best-effort label/handle from *settings*). This guarantees a backfill
    target for per-platform schema migrations regardless of whether credentials
    are currently present.
    """
    row = conn.execute(
        "SELECT account_id FROM accounts WHERE platform = ? AND is_default = 1",
        (platform,),
    ).fetchone()
    if row:
        return row["account_id"]
    if not create:
        return None
    if settings is None:
        try:
            import config
            settings = config.get_settings()
        except Exception:
            settings = {}
    label = "%s (default)" % PLATFORM_NAMES.get(platform, platform)
    handle = _default_handle(platform, settings)
    cur = conn.execute(
        "INSERT INTO accounts (platform, label, handle, enabled, is_default, sort_order)"
        " VALUES (?, ?, ?, 1, 1, 0)",
        (platform, label, handle),
    )
    # Commit immediately: most callers (pollers, posting, the server poll-loop
    # seed) close their connection without committing, so without this the new
    # default account silently rolls back — the bug that left tw/bsky with creds
    # but no account row. create_account() commits for the same reason.
    conn.commit()
    return cur.lastrowid


def resolve_account_by_identity(conn: sqlite3.Connection, platform: str,
                                handle: str | None) -> int | None:
    """Resolve ``(platform, handle)`` to a LOCAL account_id, or None.

    The natural key for an account across two installs. ``account_id`` cannot
    be that key: both installs run ``seed_default_accounts`` against their own
    AUTOINCREMENT sequence, so the same id routinely means a different account
    on each machine — measured on this pair, the server reads
    ``3=ws 4=sf 5=sqw 6=ao3`` while the desktop reads ``3=sf 4=sqw 5=ao3 6=wp``.
    Anything crossing the wire that is keyed on a raw id is therefore a
    mis-attribution waiting to happen, which is exactly what corrupted four
    live account rows on 2026-08-12.

    Resolution order deliberately matches ``apply_manifest``: exact
    case-folded ``(platform, handle)`` first, then the platform default. The
    fallback earns its place because a seeded default carries no handle until
    the platform is connected, so the handle key cannot see it, while a partial
    unique index already guarantees at most one default per platform.

    Returns None rather than guessing when the platform is unknown locally —
    the caller decides whether that is a hard error, because attributing a post
    to the wrong account is worse than refusing to record it.
    """
    handle = (handle or "").strip()
    if handle:
        row = conn.execute(
            "SELECT account_id FROM accounts WHERE platform = ? "
            "AND lower(trim(handle)) = ?",
            (platform, handle.lower()),
        ).fetchone()
        if row:
            return row["account_id"]
    return get_default_account_id(conn, platform)


def seed_default_accounts(conn: sqlite3.Connection, settings: dict) -> int:
    """Create a default account for every platform that currently has creds.

    Returns the number of default accounts created. Idempotent: a platform that
    already has a default account is skipped. Run once during migration so
    existing single-account installs gain their default account rows.
    """
    created = 0
    for platform in PLATFORMS:
        check = DEFAULT_CRED_CHECKS.get(platform)
        if not check or not check(settings):
            continue
        if get_default_account_id(conn, platform) is not None:
            continue
        get_default_account_id(conn, platform, create=True, settings=settings)
        created += 1
    return created


def account_stats(conn: sqlite3.Connection, account_id: int, platform: str) -> dict | None:
    """Return {submissions, views, favorites, comments, score} for one account.

    None if the platform is unknown, or its submissions table isn't
    account-aware yet. Used to show per-account stats on the Accounts page so
    two accounts' numbers appear side by side, and pooled per identity by
    :func:`personas.persona_stats`.

    Metric columns come from the registry (database/platform_metrics.py). This
    used to sniff for `views`/`favorites_count`/`comments_count` and write 0
    for anything else, so every account on Twitter, e621, Itaku, Bluesky,
    Mastodon or Tumblr reported zero favourites — their columns are called
    likes/notes/score — and the "By persona" widget under-counted to match.
    """
    spec = platform_metrics.get(platform)
    if not spec:
        return None
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({spec.table})").fetchall()}
    if "account_id" not in cols:
        return None

    def _sum(col: str | None, alias: str) -> str:
        # `col in cols` keeps this safe on a DB where a migration hasn't landed.
        return (f"COALESCE(SUM({col}), 0) AS {alias}"
                if col and col in cols else f"0 AS {alias}")

    parts = [
        "COUNT(*) AS submissions",
        _sum(spec.views, "views"),
        _sum(spec.faves, "favorites"),
        _sum(spec.comments, "comments"),
        # Score rides in its own column so a booru's net up−down total is never
        # mistaken for a view count.
        _sum(spec.score, "score"),
    ]
    row = conn.execute(
        f"SELECT {', '.join(parts)} FROM {spec.table} WHERE account_id = ?",
        (account_id,),
    ).fetchone()
    return dict(row) if row else None


# ── CRUD ───────────────────────────────────────────────────────

def list_accounts(conn: sqlite3.Connection, platform: str | None = None,
                  enabled_only: bool = False) -> list[dict]:
    sql = "SELECT * FROM accounts"
    clauses, params = [], []
    if platform:
        clauses.append("platform = ?")
        params.append(platform)
    if enabled_only:
        clauses.append("enabled = 1")
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY platform, is_default DESC, sort_order, account_id"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def get_account(conn: sqlite3.Connection, account_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM accounts WHERE account_id = ?", (account_id,)).fetchone()
    return dict(row) if row else None


def create_account(conn: sqlite3.Connection, platform: str, label: str,
                   handle: str = "", enabled: bool = True,
                   is_default: bool = False) -> int:
    """Insert an account and return its account_id.

    If *is_default* is requested but the platform already has a default, the new
    account is created as non-default instead (the partial unique index would
    otherwise reject it).
    """
    if is_default and get_default_account_id(conn, platform) is not None:
        is_default = False
    cur = conn.execute(
        "INSERT INTO accounts (platform, label, handle, enabled, is_default, sort_order)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (platform, label or "%s account" % PLATFORM_NAMES.get(platform, platform),
         handle, 1 if enabled else 0, 1 if is_default else 0,
         _next_sort_order(conn, platform)),
    )
    conn.commit()
    return cur.lastrowid


def _next_sort_order(conn: sqlite3.Connection, platform: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(sort_order), -1) + 1 AS n FROM accounts WHERE platform = ?",
        (platform,),
    ).fetchone()
    return row["n"] if row else 0


def update_account(conn: sqlite3.Connection, account_id: int, **fields) -> bool:
    """Update label/handle/enabled/sort_order on an account. Returns True if a row changed."""
    allowed = {"label", "handle", "enabled", "sort_order"}
    sets, params = [], []
    for key, val in fields.items():
        if key not in allowed or val is None:
            continue
        if key == "enabled":
            val = 1 if val else 0
        sets.append(f"{key} = ?")
        params.append(val)
    if not sets:
        return False
    params.append(account_id)
    cur = conn.execute(f"UPDATE accounts SET {', '.join(sets)} WHERE account_id = ?", params)
    conn.commit()
    return cur.rowcount > 0


def delete_account(conn: sqlite3.Connection, account_id: int) -> bool:
    """Delete an account row. Callers must guard against deleting a default
    account (the API layer re-promotes or refuses). Does NOT cascade to the
    per-platform analytics rows — those are left orphaned-by-account_id, which
    is harmless (they simply stop being shown)."""
    cur = conn.execute("DELETE FROM accounts WHERE account_id = ?", (account_id,))
    conn.commit()
    return cur.rowcount > 0


def count_accounts(conn: sqlite3.Connection, platform: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM accounts WHERE platform = ?", (platform,)
    ).fetchone()
    return row["n"] if row else 0


# ── Sync manifest (desktop ↔ server account parity) ────────────
# The accounts table is DB state, not settings, so it does not ride the normal
# settings-sync channel. We serialize it into a settings key (``_accounts_manifest``)
# that the sync layer carries, and re-materialize it on the other side. Apply is
# an ADDITIVE upsert (never deletes) so a stale side can't wipe the other's
# accounts; credential values themselves still travel as flat/prefixed keys.

def get_manifest(conn: sqlite3.Connection) -> list[dict]:
    return [
        {k: r[k] for k in ("account_id", "platform", "label", "handle",
                           "enabled", "is_default", "sort_order", "persona_id")}
        for r in conn.execute("SELECT * FROM accounts ORDER BY account_id").fetchall()
    ]


# ── Why the manifest is matched on (platform, handle), not on account_id ──
#
# ``account_id`` is AUTOINCREMENT and both installs allocate from their own
# sequence: ``seed_default_accounts`` runs at boot on the desktop AND the server
# (``main.py``, ``server.py``), so the same id means a DIFFERENT account on each
# box the moment the two seed lists diverge by even one row.
#
# This is not hypothetical. On 2026-08-12 pairing a desktop corrupted four
# server rows: the ids were offset by one from id 3, and the old upsert was
#
#     ON CONFLICT(account_id) DO UPDATE SET label, handle, enabled, sort_order
#
# which never checked ``platform``. Server id 3 kept ``platform='ws'`` while
# taking the desktop's SoFurry label and handle — an account holding one
# platform's identity and another's credentials-facing name.
#
# That is far worse than a cosmetic mixup: ``account_id`` threads through ~75
# columns and sits INSIDE ``publications``' UNIQUE(content_type, story_name,
# chapter_index, platform, account_id), so shifting an account's identity
# silently changes what those rows mean. ``handle`` is not cosmetic either —
# own-comment detection matches on it.
#
# So resolution is now, in order:
#   1. (platform, handle) — the natural key. Survives an id offset, which is
#      exactly the case that broke. Only used when the handle is non-empty.
#   2. (platform, is_default) — the seeded default accounts carry no handle
#      until the platform is connected, so rule 1 can't see them; but a partial
#      unique index already guarantees at most one default per platform, which
#      makes this a real key. This is the rule that resolves the incident rows.
#   3. account_id, but ONLY if the local row's platform matches. Keeps ids
#      stable for the normal aligned case (and for restore-from-manifest).
#   4. otherwise INSERT — with the manifest's id when free, a fresh one when not.
#
# An id that belongs to a different platform locally never writes through; the
# row is inserted as new instead. Inserting is the safe failure: a spurious
# duplicate is visible and deletable, whereas a clobbered identity is silent and
# spreads through every table keyed on account_id. Collisions are logged.
# ``platform`` itself is never updated: it is the identity, not an attribute.

def apply_manifest(conn: sqlite3.Connection, manifest) -> int:
    """Upsert accounts from a sync manifest. Additive only (no deletes).

    Returns the number of rows inserted or updated. Rows whose ``account_id``
    already belongs to a *different platform* locally are skipped rather than
    overwritten — see the note above.
    """
    if isinstance(manifest, str):
        try:
            manifest = json.loads(manifest)
        except (ValueError, TypeError):
            return 0
    if not isinstance(manifest, list):
        return 0

    local = conn.execute(
        "SELECT account_id, platform, handle, is_default FROM accounts").fetchall()
    by_id = {r["account_id"]: r["platform"] for r in local}
    # Handles are compared case-folded: platforms are inconsistent about the
    # case they report, and a case-only difference is the same account.
    by_natural = {
        (r["platform"], (r["handle"] or "").strip().lower()): r["account_id"]
        for r in local if (r["handle"] or "").strip()
    }
    by_default = {r["platform"]: r["account_id"] for r in local if r["is_default"]}

    n = 0
    conflicts = []
    for acct in manifest:
        try:
            aid = int(acct["account_id"])
            platform = acct["platform"]
        except (KeyError, TypeError, ValueError):
            continue
        handle = (acct.get("handle") or "").strip()
        incoming_default = int(acct.get("is_default", 0))

        target = by_natural.get((platform, handle.lower())) if handle else None
        if target is None and incoming_default:
            target = by_default.get(platform)
        if target is None:
            existing_platform = by_id.get(aid)
            if existing_platform is not None and existing_platform != platform:
                # The incident case. Writing through here would give a live row
                # one platform's identity and another's name; insert instead.
                conflicts.append((aid, existing_platform, platform))
            elif existing_platform is not None:
                target = aid

        # Don't let an incoming default collide with an existing different one —
        # the partial unique index would reject the write outright.
        is_default = incoming_default
        if is_default:
            held_by = by_default.get(platform)
            if held_by is not None and held_by != target:
                is_default = 0

        if target is None:
            # New locally. Keep the manifest's id so a restore-from-manifest
            # round-trips, but only if that id is genuinely free.
            new_id = aid if aid not in by_id else None
            if new_id is None:
                cur = conn.execute(
                    "INSERT INTO accounts (platform, label, handle, enabled, is_default, sort_order)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (platform, acct.get("label", ""), handle, int(acct.get("enabled", 1)),
                     is_default, int(acct.get("sort_order", 0))),
                )
                target = cur.lastrowid
            else:
                conn.execute(
                    "INSERT INTO accounts (account_id, platform, label, handle, enabled, is_default, sort_order)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (new_id, platform, acct.get("label", ""), handle,
                     int(acct.get("enabled", 1)), is_default, int(acct.get("sort_order", 0))),
                )
                target = new_id
            by_id[target] = platform
            if handle:
                by_natural[(platform, handle.lower())] = target
            if is_default:
                by_default[platform] = target
        else:
            conn.execute(
                "UPDATE accounts SET label = ?, handle = ?, enabled = ?, sort_order = ?"
                " WHERE account_id = ?",
                (acct.get("label", ""), handle, int(acct.get("enabled", 1)),
                 int(acct.get("sort_order", 0)), target),
            )
            if handle:
                by_natural[(platform, handle.lower())] = target

        # Persona assignment: only touch persona_id when the manifest actually
        # carries the key (present-but-null = explicit unassign; absent = an old
        # client, so leave the local assignment alone rather than clobber it).
        if "persona_id" in acct:
            conn.execute("UPDATE accounts SET persona_id = ? WHERE account_id = ?",
                         (acct.get("persona_id"), target))
        n += 1

    conn.commit()
    if conflicts:
        # Loud: the two installs disagree about what an id means. Nothing was
        # corrupted (these were inserted, not written through), but the ids have
        # drifted and someone should reconcile them deliberately.
        logger.warning(
            "accounts manifest: %d incoming row(s) carried an account_id that belongs "
            "to a different platform locally; inserted as new instead of overwriting: %s",
            len(conflicts),
            ", ".join(f"id={a} local={lp!r} incoming={ip!r}" for a, lp, ip in conflicts),
        )
    return n
