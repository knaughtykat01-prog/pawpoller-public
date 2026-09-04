"""Database connection manager and schema initialization.

This module handles all SQLite setup: connection creation, schema loading from
external .sql files, and incremental migrations for databases created by older
versions of PawPoller.

Key design decisions:
- WAL journal mode for concurrent read/write access (the UI can read while
  a background poller writes without locking).
- Row factory set to sqlite3.Row so query results behave like dicts
  (row["column_name"]) rather than bare tuples.
- Schema files are loaded via config.resource_path() which resolves paths
  correctly both in development and in PyInstaller frozen builds (_internal/).
- Foreign keys are enabled per-connection (SQLite disables them by default).
- Migrations use a "check table existence, then CREATE IF NOT EXISTS" pattern
  so they are idempotent and safe to re-run on every startup.
"""

import logging
import re
import sqlite3
from pathlib import Path

import config

logger = logging.getLogger(__name__)

# Schema file paths resolved via resource_path(). In development these point to
# the source tree; in a PyInstaller frozen build they resolve into the _internal/
# directory bundled alongside the executable. This ensures the app can always
# find its schema files regardless of deployment method.
_SCHEMA_PATH = config.resource_path("database/schema.sql")       # Inkbunny (primary platform)
_FA_SCHEMA_PATH = config.resource_path("database/fa_schema.sql") # FurAffinity tables
_WS_SCHEMA_PATH = config.resource_path("database/ws_schema.sql") # Weasyl tables
_SF_SCHEMA_PATH = config.resource_path("database/sf_schema.sql") # SoFurry tables
_SQW_SCHEMA_PATH = config.resource_path("database/sqw_schema.sql") # SquidgeWorld tables
_AO3_SCHEMA_PATH = config.resource_path("database/ao3_schema.sql") # AO3 tables
_DA_SCHEMA_PATH = config.resource_path("database/da_schema.sql")   # DeviantArt tables
_WP_SCHEMA_PATH = config.resource_path("database/wp_schema.sql")   # Wattpad tables
_IK_SCHEMA_PATH = config.resource_path("database/ik_schema.sql")   # Itaku tables
_BSKY_SCHEMA_PATH = config.resource_path("database/bsky_schema.sql")  # Bluesky tables
_TW_SCHEMA_PATH = config.resource_path("database/tw_schema.sql")      # X/Twitter tables
_MAST_SCHEMA_PATH = config.resource_path("database/mast_schema.sql")  # Mastodon tables
_TUM_SCHEMA_PATH = config.resource_path("database/tum_schema.sql")    # Tumblr tables
_PIX_SCHEMA_PATH = config.resource_path("database/pix_schema.sql")    # Pixiv tables
_THR_SCHEMA_PATH = config.resource_path("database/thr_schema.sql")    # Threads tables
_IG_SCHEMA_PATH = config.resource_path("database/ig_schema.sql")      # Instagram tables
_E621_SCHEMA_PATH = config.resource_path("database/e621_schema.sql")  # e621 tables
_FN_SCHEMA_PATH = config.resource_path("database/fn_schema.sql")      # FurryNetwork tables
_FBR_SCHEMA_PATH = config.resource_path("database/fbr_schema.sql")    # Furbooru tables
_TG_SCHEMA_PATH = config.resource_path("database/tg_schema.sql")      # Telegram tables
_POSTING_SCHEMA_PATH = config.resource_path("database/posting_schema.sql")  # Posting module tables
_POSTS_SCHEMA_PATH = config.resource_path("database/posts_schema.sql")      # Posts (microblog) module tables
_COLLECTIONS_SCHEMA_PATH = config.resource_path("database/collections_schema.sql")  # Collections (master container) tables
_COMMISSIONS_SCHEMA_PATH = config.resource_path("database/commissions_schema.sql")  # Commissions (client tracker) tables


def get_connection() -> sqlite3.Connection:
    """Get a new SQLite connection with WAL mode and row factory.

    Every connection is configured with:
    - WAL (Write-Ahead Logging) journal mode: allows concurrent readers and a
      single writer without blocking each other. This is critical because the
      GUI thread reads data for display while the poller thread writes new
      snapshots simultaneously. Without WAL, readers would be blocked during
      writes (or vice versa), causing UI freezes.
    - Row factory set to sqlite3.Row: makes query results accessible by column
      name (row["title"]) in addition to index (row[0]). This improves code
      readability and resilience to column-order changes.
    - Foreign key enforcement: SQLite does not enforce FOREIGN KEY constraints
      by default -- it must be explicitly enabled per-connection via PRAGMA.
      This ensures referential integrity (e.g. snapshots cannot reference a
      non-existent submission_id).
    """
    conn = sqlite3.connect(str(config.DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")  # 30s retry on lock contention
    return conn


def _apply_pending_mirror_snapshot() -> None:
    """Swap in a mirror-pulled database if one is waiting; never raise.

    Imported lazily so `database.db` keeps no import-time dependency on the
    mirror package, and wrapped because a failure here must not stop the app
    booting -- `apply_pending_snapshot` already refuses to touch the live
    database unless the incoming one passes an integrity check, so the worst
    case is starting on the existing database with the pending file discarded.
    """
    try:
        from mirror.core import apply_pending_snapshot
        result = apply_pending_snapshot(Path(config.DB_PATH))
    except Exception as e:  # pragma: no cover - defensive
        logger.error("Mirror: could not apply pending snapshot: %s", e, exc_info=True)
        return
    if result and result.get("applied"):
        logger.warning("Mirror: database replaced from server snapshot on startup")


def init_db() -> None:
    """Create tables if they don't exist.

    Loads and executes all three platform schema files (Inkbunny, FurAffinity,
    Weasyl) then runs incremental migrations for any tables added after the
    initial schema. Each schema file uses CREATE TABLE IF NOT EXISTS, making
    this safe to call on every application startup -- existing tables are
    left untouched.
    """
    # 3.6.0: a mirror pull stages the server's database as `pawpoller.db.pending`
    # rather than swapping it live -- SQLite cannot be replaced under a running
    # app on Windows, and a reader mid-query would otherwise find a different
    # database than it opened. Applying it HERE, at the top of the first
    # function that touches the DB, is what makes the swap atomic with respect
    # to the app: it either completed before any connection existed or it did
    # not happen. Placing it in main.py/server.py instead would leave the two
    # entry points free to drift, and would skip it in tests entirely.
    _apply_pending_mirror_snapshot()

    schema_sql = _SCHEMA_PATH.read_text(encoding="utf-8")
    fa_schema_sql = _FA_SCHEMA_PATH.read_text(encoding="utf-8")
    ws_schema_sql = _WS_SCHEMA_PATH.read_text(encoding="utf-8")
    sf_schema_sql = _SF_SCHEMA_PATH.read_text(encoding="utf-8")
    sqw_schema_sql = _SQW_SCHEMA_PATH.read_text(encoding="utf-8")
    ao3_schema_sql = _AO3_SCHEMA_PATH.read_text(encoding="utf-8")
    da_schema_sql = _DA_SCHEMA_PATH.read_text(encoding="utf-8")
    wp_schema_sql = _WP_SCHEMA_PATH.read_text(encoding="utf-8")
    conn = get_connection()
    try:
        # Execute each platform's schema in order.
        conn.executescript(schema_sql)
        conn.executescript(fa_schema_sql)
        conn.executescript(ws_schema_sql)
        conn.executescript(sf_schema_sql)
        conn.executescript(sqw_schema_sql)
        conn.executescript(ao3_schema_sql)
        conn.executescript(da_schema_sql)
        conn.executescript(wp_schema_sql)
        ik_schema_sql = _IK_SCHEMA_PATH.read_text(encoding="utf-8")
        conn.executescript(ik_schema_sql)
        bsky_schema_sql = _BSKY_SCHEMA_PATH.read_text(encoding="utf-8")
        conn.executescript(bsky_schema_sql)
        tw_schema_sql = _TW_SCHEMA_PATH.read_text(encoding="utf-8")
        conn.executescript(tw_schema_sql)
        mast_schema_sql = _MAST_SCHEMA_PATH.read_text(encoding="utf-8")
        conn.executescript(mast_schema_sql)
        tum_schema_sql = _TUM_SCHEMA_PATH.read_text(encoding="utf-8")
        conn.executescript(tum_schema_sql)
        pix_schema_sql = _PIX_SCHEMA_PATH.read_text(encoding="utf-8")
        conn.executescript(pix_schema_sql)
        thr_schema_sql = _THR_SCHEMA_PATH.read_text(encoding="utf-8")
        conn.executescript(thr_schema_sql)
        ig_schema_sql = _IG_SCHEMA_PATH.read_text(encoding="utf-8")
        conn.executescript(ig_schema_sql)
        e621_schema_sql = _E621_SCHEMA_PATH.read_text(encoding="utf-8")
        conn.executescript(e621_schema_sql)
        fn_schema_sql = _FN_SCHEMA_PATH.read_text(encoding="utf-8")
        conn.executescript(fn_schema_sql)
        fbr_schema_sql = _FBR_SCHEMA_PATH.read_text(encoding="utf-8")
        conn.executescript(fbr_schema_sql)
        tg_schema_sql = _TG_SCHEMA_PATH.read_text(encoding="utf-8")
        conn.executescript(tg_schema_sql)
        posting_schema_sql = _POSTING_SCHEMA_PATH.read_text(encoding="utf-8")
        conn.executescript(posting_schema_sql)
        posts_schema_sql = _POSTS_SCHEMA_PATH.read_text(encoding="utf-8")
        conn.executescript(posts_schema_sql)
        collections_schema_sql = _COLLECTIONS_SCHEMA_PATH.read_text(encoding="utf-8")
        conn.executescript(collections_schema_sql)
        commissions_schema_sql = _COMMISSIONS_SCHEMA_PATH.read_text(encoding="utf-8")
        conn.executescript(commissions_schema_sql)
        # Apply any migrations for tables added after the original schema release.
        _run_migrations(conn)
        conn.commit()
    finally:
        conn.close()

    # Constraint-changing table rebuilds for multi-account (session_cache PK,
    # watchers/publications UNIQUE). These run on a dedicated FK-off connection
    # because SQLite can only toggle foreign-key enforcement outside a
    # transaction, and dropping the FK-referenced publications table with FK on
    # would trigger an implicit DELETE that violates posting_queue/posting_log.
    _run_table_rebuilds()


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _add_account_id_and_backfill(conn: sqlite3.Connection, _accounts, platform: str,
                                 tables: list[str], data_check_table: str | None = None) -> None:
    """Additive account_id column + backfill to the platform's default account.

    Generic version of the inline IB/FA blocks, used to roll the analytics
    account_id columns out to additional platforms. account_id=0 is the "unset"
    sentinel; after backfill no row is 0. Idempotent.
    """
    existing = [t for t in tables if _table_exists(conn, t)]
    for t in existing:
        try:
            conn.execute(f"ALTER TABLE {t} ADD COLUMN account_id INTEGER NOT NULL DEFAULT 0")
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e).lower():
                raise
    default_id = _accounts.get_default_account_id(conn, platform)
    if default_id is None and data_check_table and data_check_table in existing:
        if conn.execute(f"SELECT 1 FROM {data_check_table} LIMIT 1").fetchone():
            default_id = _accounts.get_default_account_id(conn, platform, create=True)
    if default_id is not None:
        for t in existing:
            conn.execute(
                f"UPDATE {t} SET account_id = ? WHERE account_id = 0 OR account_id IS NULL",
                (default_id,))


def _run_table_rebuilds() -> None:
    """Constraint-changing table rebuilds for multi-account support.

    Runs on a dedicated autocommit connection with foreign keys OFF. Each
    rebuild is idempotent (guarded on the presence of the account_id column) and
    cleans up any leftover ``*_new`` table from an interrupted prior run.
    """
    conn = sqlite3.connect(str(config.DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.isolation_level = None  # autocommit so PRAGMA foreign_keys toggling applies
    try:
        from database import accounts as _accounts
        _accounts.ensure_accounts_table(conn)
        conn.execute("PRAGMA foreign_keys=OFF")
        _rebuild_session_cache(conn, _accounts)
        _rebuild_watchers(conn, _accounts)
        _rebuild_fa_watchers(conn, _accounts)
        _rebuild_sf_watchers(conn, _accounts)
        _rebuild_publications(conn, _accounts)
        _rebuild_publications_content_type(conn, _accounts)
    finally:
        try:
            conn.execute("PRAGMA foreign_keys=ON")
        except sqlite3.Error:
            pass
        conn.close()


def _rebuild_session_cache(conn: sqlite3.Connection, _accounts) -> None:
    """session_cache singleton (CHECK id=1) → one row per account (PK account_id)."""
    if not _table_exists(conn, "session_cache"):
        return
    cols = {r[1] for r in conn.execute("PRAGMA table_info(session_cache)").fetchall()}
    if "account_id" in cols:
        return  # already migrated
    user_id_expr = "user_id" if "user_id" in cols else "0"
    old = conn.execute(
        f"SELECT sid, username, {user_id_expr} AS user_id, created_at "
        f"FROM session_cache WHERE id = 1"
    ).fetchone()
    target = _accounts.get_default_account_id(conn, "ib")
    if old is not None and target is None:
        target = _accounts.get_default_account_id(conn, "ib", create=True)
    conn.execute("DROP TABLE IF EXISTS session_cache_new")
    conn.execute(
        """CREATE TABLE session_cache_new (
            account_id INTEGER PRIMARY KEY,
            sid        TEXT NOT NULL,
            username   TEXT NOT NULL,
            user_id    INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )"""
    )
    if old is not None and target is not None:
        conn.execute(
            "INSERT INTO session_cache_new (account_id, sid, username, user_id, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (target, old["sid"], old["username"], old["user_id"], old["created_at"]),
        )
    conn.execute("DROP TABLE session_cache")
    conn.execute("ALTER TABLE session_cache_new RENAME TO session_cache")
    logger.info("Rebuilt session_cache for multi-account (PK account_id)")


def _rebuild_watchers(conn: sqlite3.Connection, _accounts) -> None:
    """watchers UNIQUE(username) → UNIQUE(account_id, username)."""
    if not _table_exists(conn, "watchers"):
        return
    cols = {r[1] for r in conn.execute("PRAGMA table_info(watchers)").fetchall()}
    if "account_id" in cols:
        return
    target = _accounts.get_default_account_id(conn, "ib")
    has_rows = conn.execute("SELECT 1 FROM watchers LIMIT 1").fetchone() is not None
    if target is None and has_rows:
        target = _accounts.get_default_account_id(conn, "ib", create=True)
    conn.execute("DROP TABLE IF EXISTS watchers_new")
    conn.execute(
        """CREATE TABLE watchers_new (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id    INTEGER NOT NULL DEFAULT 0,
            user_id       INTEGER NOT NULL DEFAULT 0,
            username      TEXT NOT NULL,
            first_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(account_id, username)
        )"""
    )
    if target is not None:
        conn.execute(
            "INSERT OR IGNORE INTO watchers_new (account_id, user_id, username, first_seen_at)"
            " SELECT ?, user_id, username, first_seen_at FROM watchers WHERE username != ''",
            (target,),
        )
    conn.execute("DROP TABLE watchers")
    conn.execute("ALTER TABLE watchers_new RENAME TO watchers")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_watchers_seen ON watchers(first_seen_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_watchers_acct ON watchers(account_id)")
    logger.info("Rebuilt watchers for multi-account (UNIQUE(account_id, username))")


def _rebuild_fa_watchers(conn: sqlite3.Connection, _accounts) -> None:
    """fa_watchers UNIQUE(username) → UNIQUE(account_id, username), preserving
    the spam-protection columns (confirmed/last_seen_at/is_spam/notified)."""
    if not _table_exists(conn, "fa_watchers"):
        return
    cols = {r[1] for r in conn.execute("PRAGMA table_info(fa_watchers)").fetchall()}
    if "account_id" in cols:
        return
    target = _accounts.get_default_account_id(conn, "fa")
    has_rows = conn.execute("SELECT 1 FROM fa_watchers LIMIT 1").fetchone() is not None
    if target is None and has_rows:
        target = _accounts.get_default_account_id(conn, "fa", create=True)
    conn.execute("DROP TABLE IF EXISTS fa_watchers_new")
    conn.execute(
        """CREATE TABLE fa_watchers_new (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id    INTEGER NOT NULL DEFAULT 0,
            username      TEXT NOT NULL,
            first_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
            confirmed     INTEGER NOT NULL DEFAULT 0,
            last_seen_at  TEXT DEFAULT (datetime('now')),
            is_spam       INTEGER NOT NULL DEFAULT 0,
            notified      INTEGER NOT NULL DEFAULT 0,
            UNIQUE(account_id, username)
        )"""
    )
    if target is not None:
        conn.execute(
            "INSERT OR IGNORE INTO fa_watchers_new "
            "(account_id, username, first_seen_at, confirmed, last_seen_at, is_spam, notified) "
            "SELECT ?, username, first_seen_at, confirmed, last_seen_at, is_spam, notified "
            "FROM fa_watchers WHERE username != ''",
            (target,),
        )
    conn.execute("DROP TABLE fa_watchers")
    conn.execute("ALTER TABLE fa_watchers_new RENAME TO fa_watchers")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_fa_watchers_seen ON fa_watchers(first_seen_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_fa_watchers_pending ON fa_watchers(confirmed, notified)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_fa_watchers_acct ON fa_watchers(account_id)")
    logger.info("Rebuilt fa_watchers for multi-account (UNIQUE(account_id, username))")


def _rebuild_sf_watchers(conn: sqlite3.Connection, _accounts) -> None:
    """sf_watchers UNIQUE(username) → UNIQUE(account_id, username)."""
    if not _table_exists(conn, "sf_watchers"):
        return
    cols = {r[1] for r in conn.execute("PRAGMA table_info(sf_watchers)").fetchall()}
    if "account_id" in cols:
        return
    target = _accounts.get_default_account_id(conn, "sf")
    has_rows = conn.execute("SELECT 1 FROM sf_watchers LIMIT 1").fetchone() is not None
    if target is None and has_rows:
        target = _accounts.get_default_account_id(conn, "sf", create=True)
    conn.execute("DROP TABLE IF EXISTS sf_watchers_new")
    conn.execute(
        """CREATE TABLE sf_watchers_new (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id    INTEGER NOT NULL DEFAULT 0,
            username      TEXT NOT NULL,
            first_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(account_id, username)
        )"""
    )
    if target is not None:
        conn.execute(
            "INSERT OR IGNORE INTO sf_watchers_new (account_id, username, first_seen_at)"
            " SELECT ?, username, first_seen_at FROM sf_watchers WHERE username != ''",
            (target,),
        )
    conn.execute("DROP TABLE sf_watchers")
    conn.execute("ALTER TABLE sf_watchers_new RENAME TO sf_watchers")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sf_watchers_acct ON sf_watchers(account_id)")
    logger.info("Rebuilt sf_watchers for multi-account (UNIQUE(account_id, username))")


def _rebuild_publications(conn: sqlite3.Connection, _accounts) -> None:
    """publications UNIQUE(story,chapter,platform) → +account_id, per-row backfill."""
    if not _table_exists(conn, "publications"):
        return
    cols = {r[1] for r in conn.execute("PRAGMA table_info(publications)").fetchall()}
    if "account_id" in cols:
        return
    conn.execute("DROP TABLE IF EXISTS publications_new")
    conn.execute(
        """CREATE TABLE publications_new (
            pub_id           INTEGER PRIMARY KEY AUTOINCREMENT,
            story_name       TEXT NOT NULL,
            chapter_index    INTEGER DEFAULT 0,
            chapter_title    TEXT DEFAULT '',
            platform         TEXT NOT NULL,
            account_id       INTEGER NOT NULL DEFAULT 0,
            external_id      TEXT NOT NULL DEFAULT '',
            external_url     TEXT DEFAULT '',
            format_file      TEXT DEFAULT '',
            file_hash        TEXT DEFAULT '',
            tags_used        TEXT DEFAULT '[]',
            title_used       TEXT DEFAULT '',
            description_used TEXT DEFAULT '',
            rating_used      TEXT DEFAULT '',
            status           TEXT NOT NULL DEFAULT 'draft',
            first_posted_at  TEXT,
            last_updated_at  TEXT,
            update_count     INTEGER DEFAULT 0,
            last_error       TEXT,
            created_at       TEXT NOT NULL DEFAULT (datetime('now')),
            word_count       INTEGER DEFAULT 0,
            UNIQUE(story_name, chapter_index, platform, account_id)
        )"""
    )
    # Preserve pub_id so posting_queue/posting_log FK references stay valid.
    conn.execute(
        """INSERT INTO publications_new
            (pub_id, story_name, chapter_index, chapter_title, platform, external_id,
             external_url, format_file, file_hash, tags_used, title_used,
             description_used, rating_used, status, first_posted_at, last_updated_at,
             update_count, last_error, created_at, word_count)
        SELECT pub_id, story_name, chapter_index, chapter_title, platform, external_id,
             external_url, format_file, COALESCE(file_hash, ''), tags_used, title_used,
             description_used, rating_used, status, first_posted_at, last_updated_at,
             update_count, last_error, COALESCE(created_at, datetime('now')), word_count
        FROM publications"""
    )
    # Backfill each row to ITS platform's default account (publications spans
    # platforms — so the target is not a single account).
    platforms = [r["platform"] for r in
                 conn.execute("SELECT DISTINCT platform FROM publications_new").fetchall()]
    for plat in platforms:
        aid = _accounts.get_default_account_id(conn, plat) \
            or _accounts.get_default_account_id(conn, plat, create=True)
        conn.execute(
            "UPDATE publications_new SET account_id = ? "
            "WHERE platform = ? AND (account_id = 0 OR account_id IS NULL)",
            (aid, plat),
        )
    conn.execute("DROP TABLE publications")
    conn.execute("ALTER TABLE publications_new RENAME TO publications")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_publications_story ON publications(story_name)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_publications_platform ON publications(platform)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_publications_status ON publications(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_publications_account ON publications(account_id)")
    logger.info("Rebuilt publications for multi-account (UNIQUE incl account_id)")


def _rebuild_publications_content_type(conn: sqlite3.Connection, _accounts) -> None:
    """publications: fold content_type into the UNIQUE key.

    Runs after _rebuild_publications (which adds account_id). An artwork can be
    named like a story (e.g. its own cover art); without content_type in the
    key, both post chapter 0 to the same platform/account and the artwork
    UPSERTs onto the story's row. Idempotent — guarded on content_type already
    being in the stored UNIQUE. Defensive about whether the column survived the
    account_id rebuild (whose fixed INSERT list drops it on legacy DBs): reads
    the live column set rather than assuming it's present.
    """
    if not _table_exists(conn, "publications"):
        return
    ddl = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='publications'"
    ).fetchone()
    if ddl and "UNIQUE(content_type" in (ddl[0] or ""):
        return  # already migrated
    cols = {r[1] for r in conn.execute("PRAGMA table_info(publications)").fetchall()}
    ct_expr = "COALESCE(content_type, 'story')" if "content_type" in cols else "'story'"
    acct_expr = "account_id" if "account_id" in cols else "0"
    conn.execute("DROP TABLE IF EXISTS publications_ct_new")
    conn.execute(
        """CREATE TABLE publications_ct_new (
            pub_id           INTEGER PRIMARY KEY AUTOINCREMENT,
            content_type     TEXT NOT NULL DEFAULT 'story',
            story_name       TEXT NOT NULL,
            chapter_index    INTEGER DEFAULT 0,
            chapter_title    TEXT DEFAULT '',
            platform         TEXT NOT NULL,
            account_id       INTEGER NOT NULL DEFAULT 0,
            external_id      TEXT NOT NULL DEFAULT '',
            external_url     TEXT DEFAULT '',
            format_file      TEXT DEFAULT '',
            file_hash        TEXT DEFAULT '',
            tags_used        TEXT DEFAULT '[]',
            title_used       TEXT DEFAULT '',
            description_used TEXT DEFAULT '',
            rating_used      TEXT DEFAULT '',
            status           TEXT NOT NULL DEFAULT 'draft',
            first_posted_at  TEXT,
            last_updated_at  TEXT,
            update_count     INTEGER DEFAULT 0,
            last_error       TEXT,
            created_at       TEXT NOT NULL DEFAULT (datetime('now')),
            word_count       INTEGER DEFAULT 0,
            UNIQUE(content_type, story_name, chapter_index, platform, account_id)
        )"""
    )
    # Preserve pub_id so posting_queue/posting_log FK references stay valid.
    conn.execute(
        f"""INSERT INTO publications_ct_new
            (pub_id, content_type, story_name, chapter_index, chapter_title, platform,
             account_id, external_id, external_url, format_file, file_hash, tags_used,
             title_used, description_used, rating_used, status, first_posted_at,
             last_updated_at, update_count, last_error, created_at, word_count)
        SELECT pub_id, {ct_expr}, story_name, chapter_index, chapter_title, platform,
             {acct_expr}, external_id, external_url, format_file, COALESCE(file_hash, ''),
             tags_used, title_used, description_used, rating_used, status, first_posted_at,
             last_updated_at, update_count, last_error, COALESCE(created_at, datetime('now')),
             word_count
        FROM publications"""
    )
    conn.execute("DROP TABLE publications")
    conn.execute("ALTER TABLE publications_ct_new RENAME TO publications")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_publications_story ON publications(story_name)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_publications_platform ON publications(platform)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_publications_status ON publications(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_publications_account ON publications(account_id)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_publications_content_type ON publications(content_type)")
    logger.info("Rebuilt publications to fold content_type into UNIQUE")


def _run_migrations(conn: sqlite3.Connection) -> None:
    """Apply schema migrations for existing databases.

    Migration pattern: query sqlite_master for existing table names, then
    conditionally create any tables that are missing. This approach:
    - Is idempotent (safe to run repeatedly on every startup).
    - Handles upgrades from any previous schema version.
    - Uses CREATE TABLE IF NOT EXISTS as a secondary safety net.

    Each migration block is documented with what it adds and why.
    """
    # Get the set of all table names currently in the database.
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}

    # Migration 0: Multi-account registry.
    # The `accounts` table is the cross-platform identity layer that lets more
    # than one account run per platform. It MUST be created and seeded before any
    # per-platform account_id backfill below, because those backfills target the
    # platform's *default* account_id (which is created here). Seeding gives every
    # platform that already has credentials a default account whose data is the
    # pre-multi-account single-account history.
    from database import accounts as _accounts
    _accounts.ensure_accounts_table(conn)
    try:
        _accounts.seed_default_accounts(conn, config.get_settings())
    except Exception as e:  # never let seeding block startup migrations
        logger.warning("Default-account seeding skipped: %s", e)

    # Migration 0b: Inkbunny account_id discriminator (additive columns).
    # Adds account_id to the IB analytics tables and backfills all existing rows
    # to the IB default account (the pre-multi-account history belongs to it).
    # The constraint-changing rebuilds (session_cache PK, watchers UNIQUE,
    # publications UNIQUE) happen separately in _run_table_rebuilds() because
    # they need FK enforcement toggled off, which is impossible inside this
    # transaction. account_id is NEVER 0 after backfill — 0 is the "unset"
    # sentinel (real account_ids start at 1, AUTOINCREMENT).
    _ib_acct_tables = ["submissions", "snapshots", "comments", "poll_log", "faving_users"]
    for _t in _ib_acct_tables:
        if _t in tables:
            try:
                conn.execute(f"ALTER TABLE {_t} ADD COLUMN account_id INTEGER NOT NULL DEFAULT 0")
            except sqlite3.OperationalError as e:
                if "duplicate column" not in str(e).lower():
                    raise
    _ib_default = _accounts.get_default_account_id(conn, "ib")
    if _ib_default is None and "submissions" in tables:
        # Existing IB data but no seeded default (e.g. creds cleared) — make one.
        if conn.execute("SELECT 1 FROM submissions LIMIT 1").fetchone():
            _ib_default = _accounts.get_default_account_id(conn, "ib", create=True)
    if _ib_default is not None:
        for _t in _ib_acct_tables:
            if _t in tables:
                conn.execute(
                    f"UPDATE {_t} SET account_id = ? WHERE account_id = 0 OR account_id IS NULL",
                    (_ib_default,))
    conn.execute("CREATE INDEX IF NOT EXISTS idx_snapshots_acct ON snapshots(account_id, submission_id, polled_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_submissions_acct ON submissions(account_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_comments_acct ON comments(account_id, first_seen_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_faving_users_acct ON faving_users(account_id, submission_id, user_id)")

    # Posting tables: additive account_id. posting_queue/posting_log carry no
    # UNIQUE on the (story, chapter, platform) tuple, so a plain column add is
    # enough; the publications UNIQUE rebuild is in _run_table_rebuilds().
    for _t in ("posting_queue", "posting_log"):
        if _t in tables:
            try:
                conn.execute(f"ALTER TABLE {_t} ADD COLUMN account_id INTEGER NOT NULL DEFAULT 0")
            except sqlite3.OperationalError as e:
                if "duplicate column" not in str(e).lower():
                    raise

    # Migration 0c: FurAffinity account_id discriminator (additive columns).
    # Mirrors the IB block. FA has no session_cache (cookie auth) but adds
    # fa_profile_stats (per-account pageviews). fa_watchers UNIQUE rebuild is in
    # _run_table_rebuilds(). Backfill to the FA default account.
    _fa_acct_tables = ["fa_submissions", "fa_snapshots", "fa_comments",
                       "fa_poll_log", "fa_profile_stats"]
    for _t in _fa_acct_tables:
        if _t in tables:
            try:
                conn.execute(f"ALTER TABLE {_t} ADD COLUMN account_id INTEGER NOT NULL DEFAULT 0")
            except sqlite3.OperationalError as e:
                if "duplicate column" not in str(e).lower():
                    raise
    _fa_default = _accounts.get_default_account_id(conn, "fa")
    if _fa_default is None and "fa_submissions" in tables:
        if conn.execute("SELECT 1 FROM fa_submissions LIMIT 1").fetchone():
            _fa_default = _accounts.get_default_account_id(conn, "fa", create=True)
    if _fa_default is not None:
        for _t in _fa_acct_tables:
            if _t in tables:
                conn.execute(
                    f"UPDATE {_t} SET account_id = ? WHERE account_id = 0 OR account_id IS NULL",
                    (_fa_default,))
    conn.execute("CREATE INDEX IF NOT EXISTS idx_fa_snapshots_acct ON fa_snapshots(account_id, submission_id, polled_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_fa_submissions_acct ON fa_submissions(account_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_fa_comments_acct ON fa_comments(account_id, first_seen_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_fa_profile_stats_acct ON fa_profile_stats(account_id, polled_at)")

    # Migration 0d: account_id rollout for the simple platforms (no watcher/
    # kudos/session tables — just submissions/snapshots/poll_log). Each is the
    # Weasyl template via the shared helper.
    for _p in ("ws", "da", "wp", "ik", "bsky", "tw", "mast", "tum", "pix", "thr", "ig", "e621"):
        _add_account_id_and_backfill(
            conn, _accounts, _p,
            [f"{_p}_submissions", f"{_p}_snapshots", f"{_p}_poll_log"],
            data_check_table=f"{_p}_submissions")
        if _table_exists(conn, f"{_p}_snapshots"):
            conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{_p}_snapshots_acct ON {_p}_snapshots(account_id, submission_id, polled_at)")
        if _table_exists(conn, f"{_p}_submissions"):
            conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{_p}_submissions_acct ON {_p}_submissions(account_id)")

    # Migration 0e: sf/sqw/ao3 (have a watcher or kudos table). sf_watchers
    # UNIQUE(username) is rebuilt in _run_table_rebuilds; the kudos tables keep
    # their UNIQUE(submission_id, username) (submission_id is unique per account)
    # and just gain an additive account_id column.
    _add_account_id_and_backfill(conn, _accounts, "sf",
        ["sf_submissions", "sf_snapshots", "sf_poll_log"],
        data_check_table="sf_submissions")
    _add_account_id_and_backfill(conn, _accounts, "sqw",
        ["sqw_submissions", "sqw_snapshots", "sqw_poll_log", "sqw_kudos_users"],
        data_check_table="sqw_submissions")
    _add_account_id_and_backfill(conn, _accounts, "ao3",
        ["ao3_submissions", "ao3_snapshots", "ao3_poll_log", "ao3_kudos_users"],
        data_check_table="ao3_submissions")
    for _p in ("sf", "sqw", "ao3"):
        if _table_exists(conn, f"{_p}_snapshots"):
            conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{_p}_snapshots_acct ON {_p}_snapshots(account_id, submission_id, polled_at)")
        if _table_exists(conn, f"{_p}_submissions"):
            conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{_p}_submissions_acct ON {_p}_submissions(account_id)")
    for _kt in ("sqw_kudos_users", "ao3_kudos_users"):
        if _table_exists(conn, _kt):
            conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{_kt}_acct ON {_kt}(account_id, submission_id)")

    # Migration 1: Inkbunny comments table.
    # Added to track individual comments on Inkbunny submissions (username,
    # text, timestamp, reply threading). This was not in the original schema
    # which only tracked comment counts in snapshots. Introduced when comment
    # scraping was added to the Inkbunny poller.
    if "comments" not in tables:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS comments (
                comment_id      INTEGER PRIMARY KEY,
                submission_id   INTEGER NOT NULL,
                username        TEXT NOT NULL DEFAULT '',
                comment_text    TEXT NOT NULL DEFAULT '',
                commented_at    TEXT,
                first_seen_at   TEXT NOT NULL DEFAULT (datetime('now')),
                is_reply        INTEGER DEFAULT 0,
                reply_to_comment_id INTEGER,
                FOREIGN KEY (submission_id) REFERENCES submissions(submission_id)
            );
            CREATE INDEX IF NOT EXISTS idx_comments_submission ON comments(submission_id);
            CREATE INDEX IF NOT EXISTS idx_comments_seen ON comments(first_seen_at);
        """)

    # Migration 2: Submission groups (cross-platform tagging/grouping).
    # Allows the user to create named groups (e.g. "Commission batch #3") and
    # assign submissions from any platform to them. The group_members table
    # links a (group_id, platform, submission_id) triple with a uniqueness
    # constraint to prevent duplicates. CASCADE delete ensures removing a group
    # also removes all its member associations.
    if "submission_groups" not in tables:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS submission_groups (
                group_id    INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT NOT NULL,
                description TEXT,
                created_at  TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS submission_group_members (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id        INTEGER REFERENCES submission_groups(group_id) ON DELETE CASCADE,
                platform        TEXT NOT NULL,
                submission_id   INTEGER NOT NULL,
                UNIQUE(group_id, platform, submission_id)
            );
        """)

    # Migration 3: Cross-platform submission links.
    # Links the "same" submission across platforms (e.g. the same artwork posted
    # to Inkbunny, FurAffinity, and Weasyl). A link_id groups multiple
    # (platform, submission_id) pairs together. This enables cross-platform
    # analytics like comparing view/fave performance of the same piece across
    # sites. CASCADE delete ensures cleaning up a link removes all its members.
    if "submission_links" not in tables:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS submission_links (
                link_id     INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at  TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS submission_link_members (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                link_id         INTEGER REFERENCES submission_links(link_id) ON DELETE CASCADE,
                platform        TEXT NOT NULL,
                submission_id   INTEGER NOT NULL,
                UNIQUE(link_id, platform, submission_id)
            );
        """)

    # ── Masterpiece index (Masterpieces Phase 0) ────────────────
    # A Masterpiece is the master record for ONE image — the image analog of a
    # story's MASTER.md. Its canonical metadata lives on disk as masterpiece.json
    # (see posting/artwork_reader.py); this is a thin, NAME-KEYED index (spec
    # docs/specs/masterpieces.md §0-A2) for fast listing + migration provenance,
    # NOT the source of truth. Cross-site membership (which uploads are the same
    # image) is the separate masterpiece_members table added in Phase 1.
    if "masterpieces" not in tables:
        conn.execute("""CREATE TABLE IF NOT EXISTS masterpieces (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            name           TEXT NOT NULL UNIQUE,
            source_link_id INTEGER,
            status         TEXT NOT NULL DEFAULT '',
            created_at     TEXT DEFAULT (datetime('now')),
            updated_at     TEXT DEFAULT (datetime('now'))
        )""")

    # Junk status (2.149.0): '' = active, 'junk' = kept-but-hidden. For pulled
    # art that isn't wanted in the grid (memes, other people's ads, retired
    # pieces) without destroying the record/folder — softer than merge/delete,
    # reversible from the Junk view. Guarded ADD COLUMN for pre-2.149 DBs.
    try:
        conn.execute("ALTER TABLE masterpieces ADD COLUMN status TEXT NOT NULL DEFAULT ''")
    except sqlite3.OperationalError as e:
        if "duplicate column" not in str(e).lower():
            raise

    # ── Masterpiece membership (Masterpieces Phase 1) ───────────
    # Which platform uploads ARE this Masterpiece (the same image posted to N
    # sites). NAME-keyed, mirroring collection_members but stat-typed like a
    # Collection's submission members: (platform, submission_id) resolves live
    # against the per-platform *_submissions tables at rollup time — no stats are
    # copied here. PK is (masterpiece_name, platform, submission_id) so re-linking
    # the same upload is idempotent (spec docs/specs/masterpieces.md §0-A2).
    #   role      — 'primary' (the source upload) vs 'crosspost'
    #   linked_via — how the link was made: 'manual' | 'phash' | 'title' | 'publish'
    if "masterpiece_members" not in tables:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS masterpiece_members (
                masterpiece_name TEXT NOT NULL,
                platform         TEXT NOT NULL,
                submission_id    TEXT NOT NULL,
                account_id       INTEGER,
                role             TEXT DEFAULT 'crosspost',
                linked_via       TEXT DEFAULT 'manual',
                variant_key      TEXT NOT NULL DEFAULT '',
                added_at         TEXT DEFAULT (datetime('now')),
                PRIMARY KEY (masterpiece_name, platform, submission_id)
            );
            CREATE INDEX IF NOT EXISTS idx_mp_members_site
                ON masterpiece_members(platform, submission_id);
            CREATE INDEX IF NOT EXISTS idx_mp_members_name
                ON masterpiece_members(masterpiece_name);
        """)

    # Variant attribution (2.158.0, spec docs/specs/masterpiece_variants.md):
    # which VARIANT of the piece a site-upload is (SFW/NSFW, censored/clean...).
    # '' = primary/unattributed. Per-variant stats = the normal member rollup
    # filtered by this key; cohort totals = all members (unchanged). The variant
    # definitions themselves live in masterpiece.json ("variants" list).
    try:
        conn.execute("ALTER TABLE masterpiece_members ADD COLUMN variant_key TEXT NOT NULL DEFAULT ''")
    except sqlite3.OperationalError as e:
        if "duplicate column" not in str(e).lower():
            raise

    # ── Ignored discovered submissions (2.140.0) ────────────────
    # A user "Ignore" list for discovered artwork tiles they never want in the
    # Artwork hub (e.g. images the pollers scraped from tweets that aren't real
    # gallery work). (platform, submission_id)-keyed; the discovered-unlinked
    # query subtracts these. Reversible — un-ignore deletes the row.
    if "ignored_submissions" not in tables:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS ignored_submissions (
                platform      TEXT NOT NULL,
                submission_id TEXT NOT NULL,
                ignored_at    TEXT DEFAULT (datetime('now')),
                PRIMARY KEY (platform, submission_id)
            );
        """)

    # ── "Not a duplicate" decisions for the Masterpiece de-dup finder (2.145.0) ─
    # When the perceptual-hash finder flags two Masterpieces as look-alikes but the
    # user says they're actually different, we remember that pair so the finder
    # never groups them again. Stored normalised (name_a < name_b).
    if "masterpiece_not_duplicate" not in tables:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS masterpiece_not_duplicate (
                name_a       TEXT NOT NULL,
                name_b       TEXT NOT NULL,
                decided_at   TEXT DEFAULT (datetime('now')),
                PRIMARY KEY (name_a, name_b)
            );
        """)

    # ── Watcher tables ──────────────────────────────────────────
    if "watchers" not in tables:
        conn.execute("""CREATE TABLE IF NOT EXISTS watchers (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id       INTEGER NOT NULL DEFAULT 0,
            username      TEXT NOT NULL,
            first_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(username)
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_watchers_seen ON watchers(first_seen_at)")

    if "fa_watchers" not in tables:
        conn.execute("""CREATE TABLE IF NOT EXISTS fa_watchers (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            username      TEXT NOT NULL,
            first_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(username)
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_fa_watchers_seen ON fa_watchers(first_seen_at)")

    # Add new_watchers_found column to poll logs.
    # Why we catch and ignore OperationalError here (and in similar blocks below):
    # SQLite's ALTER TABLE ADD COLUMN throws OperationalError if the column
    # already exists, and there is no IF NOT EXISTS syntax for ALTER TABLE.
    # The try/except pattern is the standard way to make column-add migrations
    # idempotent.  We only re-raise if the error is NOT "duplicate column" to
    # catch genuine issues (e.g. disk full, locked database).
    try:
        conn.execute("ALTER TABLE poll_log ADD COLUMN new_watchers_found INTEGER DEFAULT 0")
    except sqlite3.OperationalError as e:
        if "duplicate column" not in str(e).lower():
            raise
    try:
        conn.execute("ALTER TABLE fa_poll_log ADD COLUMN new_watchers_found INTEGER DEFAULT 0")
    except sqlite3.OperationalError as e:
        if "duplicate column" not in str(e).lower():
            raise

    # Add new_comments_found column to IB poll_log
    try:
        conn.execute("ALTER TABLE poll_log ADD COLUMN new_comments_found INTEGER DEFAULT 0")
    except sqlite3.OperationalError as e:
        if "duplicate column" not in str(e).lower():
            raise

    # Migration: Goals table for tracking progress toward user-defined targets
    if "goals" not in tables:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS goals (
                goal_id       INTEGER PRIMARY KEY AUTOINCREMENT,
                platform      TEXT NOT NULL,
                scope         TEXT NOT NULL DEFAULT 'account',
                submission_id INTEGER,
                metric        TEXT NOT NULL,
                target_value  INTEGER NOT NULL,
                created_at    TEXT DEFAULT (datetime('now')),
                completed_at  TEXT
            );
        """)

    # Migration: Tags and submission_tags for user-defined categorisation
    if "tags" not in tables:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS tags (
                tag_id   INTEGER PRIMARY KEY AUTOINCREMENT,
                name     TEXT NOT NULL UNIQUE,
                color    TEXT DEFAULT '#6c8cff'
            );
            CREATE TABLE IF NOT EXISTS submission_tags (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                tag_id        INTEGER REFERENCES tags(tag_id) ON DELETE CASCADE,
                platform      TEXT NOT NULL,
                submission_id INTEGER NOT NULL,
                UNIQUE(tag_id, platform, submission_id)
            );
        """)

    # Migration: Rebuild watchers table to use UNIQUE(username) instead of UNIQUE(user_id).
    # The usersviewall page does not expose user_id, so username is the natural key.
    # Check if the old schema is in use by looking for UNIQUE(user_id) constraint.
    watcher_schema = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='watchers'"
    ).fetchone()
    if watcher_schema and "UNIQUE(user_id)" in (watcher_schema[0] or ""):
        logger.info("Migrating watchers table: UNIQUE(user_id) -> UNIQUE(username)")
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS watchers_new (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id       INTEGER NOT NULL DEFAULT 0,
                username      TEXT NOT NULL,
                first_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(username)
            );
            INSERT OR IGNORE INTO watchers_new (user_id, username, first_seen_at)
                SELECT user_id, username, first_seen_at FROM watchers WHERE username != '';
            DROP TABLE watchers;
            ALTER TABLE watchers_new RENAME TO watchers;
            CREATE INDEX IF NOT EXISTS idx_watchers_seen ON watchers(first_seen_at);
        """)

    # Migration: Add confirmation/spam columns to fa_watchers for spam protection.
    # - confirmed: 0 = pending (first seen this cycle), 1 = confirmed (still present next cycle)
    # - last_seen_at: tracks when the watcher was last seen in FAExport's list
    # - is_spam: heuristic or profile-sniff flagged as bot/spam
    # - notified: whether we've already sent a notification for this watcher
    fa_watcher_cols = {r[1] for r in conn.execute("PRAGMA table_info(fa_watchers)").fetchall()}
    if "confirmed" not in fa_watcher_cols:
        logger.info("Migrating fa_watchers: adding confirmed/last_seen_at/is_spam/notified columns")
        try:
            conn.execute("ALTER TABLE fa_watchers ADD COLUMN confirmed INTEGER DEFAULT 1")
        except sqlite3.OperationalError as e:
            logger.debug("ALTER TABLE fa_watchers (confirmed): %s", e)
        try:
            conn.execute("ALTER TABLE fa_watchers ADD COLUMN last_seen_at TEXT DEFAULT (datetime('now'))")
        except sqlite3.OperationalError as e:
            logger.debug("ALTER TABLE fa_watchers (last_seen_at): %s", e)
        try:
            conn.execute("ALTER TABLE fa_watchers ADD COLUMN is_spam INTEGER DEFAULT 0")
        except sqlite3.OperationalError as e:
            logger.debug("ALTER TABLE fa_watchers (is_spam): %s", e)
        try:
            conn.execute("ALTER TABLE fa_watchers ADD COLUMN notified INTEGER DEFAULT 1")
        except sqlite3.OperationalError as e:
            logger.debug("ALTER TABLE fa_watchers (notified): %s", e)
        # Mark all existing watchers as confirmed+notified (they're from before this feature)
        conn.execute("UPDATE fa_watchers SET confirmed = 1, notified = 1, last_seen_at = first_seen_at WHERE confirmed IS NULL OR confirmed = 1")
    # Ensure the pending-watchers index exists (created here rather than in fa_schema.sql
    # because existing databases need the migration above to add the columns first).
    try:
        conn.execute("CREATE INDEX IF NOT EXISTS idx_fa_watchers_pending ON fa_watchers(confirmed, notified)")
    except sqlite3.OperationalError:
        pass  # columns not yet present (shouldn't happen but safe fallback)

    # Migration: Add new_watchers_found column to sf_poll_log
    try:
        conn.execute("ALTER TABLE sf_poll_log ADD COLUMN new_watchers_found INTEGER DEFAULT 0")
    except sqlite3.OperationalError as e:
        if "duplicate column" not in str(e).lower():
            raise

    # Migration: Add 'requires' column to posting_queue for desktop/server mode
    if "posting_queue" in tables:
        try:
            conn.execute("ALTER TABLE posting_queue ADD COLUMN requires TEXT DEFAULT 'any'")
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e).lower():
                raise

    # Migration: Add 'file_hash' column to publications for change detection
    if "publications" in tables:
        try:
            conn.execute("ALTER TABLE publications ADD COLUMN file_hash TEXT DEFAULT ''")
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e).lower():
                raise

    # Migration (2.91.0 bsky / 2.93.0 tw+ig): 'media_urls' — a JSON array of the
    # full-res URL for every image in a multi-image post (Bluesky images, X photos,
    # Instagram carousel children), so the artwork importer can bring in all of
    # them (one artwork per image), not just the first. Existing rows stay '' until
    # re-polled (a Full Resync backfills them).
    for _mt in ("bsky_submissions", "tw_submissions", "ig_submissions"):
        if _mt in tables:
            try:
                conn.execute(f"ALTER TABLE {_mt} ADD COLUMN media_urls TEXT DEFAULT ''")
            except sqlite3.OperationalError as e:
                if "duplicate column" not in str(e).lower():
                    raise

    # Migration (2.96.0): ONE-TIME backfill of publications.account_id from the
    # source submission. Imports/links used to drop the account, so every work
    # landed on the platform's default account, burying the other personas'
    # attribution. Re-point each publication to the account that owns its
    # matching {platform}_submissions row. Guarded by a meta flag so a later
    # manual re-attribution isn't reverted on the next boot.
    if "publications" in tables:
        conn.execute("CREATE TABLE IF NOT EXISTS pp_meta (key TEXT PRIMARY KEY, value TEXT)")
        _done = conn.execute(
            "SELECT 1 FROM pp_meta WHERE key = 'pub_account_backfill_v1'").fetchone()
        if not _done:
            from posting.sync import PLATFORM_TABLES
            for _code, _cfg in PLATFORM_TABLES.items():
                _tbl, _idc = _cfg["table"], _cfg["id_col"]
                if _tbl not in tables:
                    continue
                try:
                    conn.execute(
                        f"""UPDATE publications SET account_id = (
                                SELECT s.account_id FROM {_tbl} s
                                WHERE s.{_idc} = publications.external_id
                            )
                            WHERE platform = ? AND external_id != '' AND EXISTS (
                                SELECT 1 FROM {_tbl} s
                                WHERE s.{_idc} = publications.external_id
                                  AND s.account_id IS NOT NULL AND s.account_id > 0
                            )""",
                        (_code,),
                    )
                except sqlite3.OperationalError:
                    pass  # table/column missing on this install — skip
            conn.execute(
                "INSERT OR REPLACE INTO pp_meta (key, value) VALUES "
                "('pub_account_backfill_v1', datetime('now'))")

    # Migration: Personas (cross-platform account grouping). A persona bundles
    # accounts across platforms into one logical identity for scoped views +
    # per-persona digests. accounts.persona_id is a SOFT reference (no SQL FK):
    # delete_persona nulls its accounts' persona_id in the CRUD layer. This is a
    # plain additive column, so it does NOT need the _run_table_rebuilds path —
    # and _run_table_rebuilds never rebuilds the accounts table, so the column
    # survives. The accounts table already exists (Migration 0 above).
    from database import personas as _personas
    _personas.ensure_personas_table(conn)
    try:
        conn.execute("ALTER TABLE accounts ADD COLUMN persona_id INTEGER")
    except sqlite3.OperationalError as e:
        if "duplicate column" not in str(e).lower():
            raise
    conn.execute("CREATE INDEX IF NOT EXISTS idx_accounts_persona ON accounts(persona_id)")

    # Migration: adopt Telegram accounts created before it was a real platform.
    #
    # Publishing to Telegram calls get_default_account_id(..., create=True),
    # which INSERTS and self-commits. So any install that posted to a channel
    # already has a `tg` row — labelled "tg (default)" because PLATFORM_NAMES
    # had no entry, and with an EMPTY handle because _HANDLE_KEYS had none.
    #
    # ⚠ This must ADOPT that row, not seed beside it. `idx_accounts_one_default`
    # is a partial unique index on (platform) WHERE is_default = 1, and
    # create_account silently downgrades is_default to 0 on conflict — so
    # seeding would produce a non-default account nobody asked for, while the
    # real one kept the useless label.
    #
    # ⚠ The empty handle is the more serious half. (platform, handle) is the
    # natural key for desktop↔server mirroring; with no handle a `tg` row can
    # only be matched by is_default, so a second channel added on one machine
    # inserts as a NEW row on the other and the two drift apart. Backfilling it
    # from the flat tg_channel setting is what makes multi-channel safe to sync.
    try:
        _tg = conn.execute(
            "SELECT account_id, label, handle, is_default FROM accounts"
            " WHERE platform = 'tg'"
        ).fetchall()
        if _tg:
            import config as _config
            _settings = _config.get_settings()
            for _row in _tg:
                _aid, _label, _handle = _row[0], _row[1] or "", _row[2] or ""
                _updates, _params = [], []
                if _label in ("", "tg (default)", "tg"):
                    _updates.append("label = ?")
                    _params.append("Telegram (default)")
                if not _handle:
                    # Only the DEFAULT account maps to the flat key; a non-default
                    # one stores its channel under acct_<id>_tg_channel.
                    # Signature is (account_id, field, is_default) — the default
                    # account's key is the bare field, others are namespaced.
                    _key = _config.account_setting_key(_aid, "tg_channel",
                                                       bool(_row[3]))
                    _ch = (_settings.get(_key) or "").strip()
                    if not _ch:
                        # Belt and braces: try both forms, since a row's
                        # is_default may not match where its channel was written.
                        _ch = ((_settings.get("tg_channel") or "").strip()
                               if _row[3] else
                               (_settings.get(f"acct_{_aid}_tg_channel") or "").strip())
                    if _ch:
                        _updates.append("handle = ?")
                        _params.append(_ch)
                if _updates:
                    _params.append(_aid)
                    conn.execute(
                        f"UPDATE accounts SET {', '.join(_updates)} WHERE account_id = ?",
                        _params)
            conn.commit()
    except sqlite3.OperationalError:
        # A fresh DB may not have the accounts table yet at this point; the
        # normal seeding path creates it correctly and needs no adoption.
        pass

    # Migration: per-persona posting defaults (gap-wave-3 §1) — plain additive
    # columns; also in ensure_personas_table for fresh installs.
    for _col in ("default_platforms TEXT NOT NULL DEFAULT ''",
                 "default_rating TEXT NOT NULL DEFAULT ''",
                 "preferred_post_time TEXT NOT NULL DEFAULT ''"):
        try:
            conn.execute(f"ALTER TABLE personas ADD COLUMN {_col}")
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e).lower():
                raise

    # Migration: content_type discriminator on the posting registry. Lets the
    # Artwork hub reuse publications/posting_queue/posting_log for image posts
    # without colliding with stories. Additive column (DEFAULT 'story' → every
    # existing row is a story) on all three tables. posting_queue/posting_log
    # carry no UNIQUE on the (story, chapter, platform) tuple, so a plain add is
    # enough; folding content_type into the publications UNIQUE happens in
    # _run_table_rebuilds() (_rebuild_publications_content_type), same as the
    # account_id rollout.
    for _t in ("publications", "posting_queue", "posting_log"):
        if _t in tables:
            try:
                conn.execute(
                    f"ALTER TABLE {_t} ADD COLUMN content_type TEXT NOT NULL DEFAULT 'story'")
            except sqlite3.OperationalError as e:
                if "duplicate column" not in str(e).lower():
                    raise

    # Migration: drip campaigns (gap G1, gap-wave-2 §3). A "drip" expands into N
    # ordinary one-off queue rows at creation; this nullable group id is what
    # lets a whole campaign be shown/cancelled as a unit. Additive; existing
    # rows are simply not part of any drip.
    if "posting_queue" in tables:
        try:
            conn.execute("ALTER TABLE posting_queue ADD COLUMN drip_group TEXT")
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e).lower():
                raise

    # Migration: threads (gap-wave-3 §4 / G8) — thread parts are child post
    # rows. Plain additive columns; also in posts_schema.sql for fresh installs.
    if "posts" in tables:
        for _col in ("parent_post_id INTEGER NOT NULL DEFAULT 0",
                     "thread_ordinal INTEGER NOT NULL DEFAULT 0"):
            try:
                conn.execute(f"ALTER TABLE posts ADD COLUMN {_col}")
            except sqlite3.OperationalError as e:
                if "duplicate column" not in str(e).lower():
                    raise

    # Migration: cross-platform follower/watcher counts. Account-level follower
    # counts are a single uniform integer per account, so they live in ONE shared
    # table keyed by the global account_id (not the per-platform submission
    # pattern). The current value is cached on accounts.follower_count for fast
    # Accounts-page reads; the time-series feeds the follower growth chart. Runs
    # after Migration 0 (accounts table exists) so the ALTER can target it.
    from database import followers as _followers
    _followers.ensure_follower_tables(conn)

    # Migration: unified comment inbox (gap G3). platform_comments (Stage-A1
    # capture for bsky/mast/e621/da) + inbox_state (handled flags for ALL
    # sources incl. the legacy IB/FA comment tables). Additive, idempotent.
    from database import inbox_queries as _inbox
    _inbox.ensure_inbox_tables(conn)

    # Migration: is_own on all three comment tables (2.192.0). A comment written
    # by the posting account is not engagement — it must not count as a new
    # comment, notify, or rank in Top Fans. Rows are kept and flagged rather
    # than dropped so the Inbox can still show them as thread context.
    #
    # The ALTER and its index live HERE together, never in a *_schema.sql: the
    # schema load runs BEFORE this function, so a schema file that indexed a
    # migration-added column would crash every upgrade (and fresh-install tests
    # would not catch it, because there the column exists by the time it runs).
    #
    # platform_comments gets the column in its CREATE TABLE too (see
    # inbox_queries.ensure_inbox_tables) for fresh installs — that CREATE is
    # IF NOT EXISTS, so upgraders would otherwise never receive it.
    for _ct in ("comments", "fa_comments", "platform_comments"):
        if not _table_exists(conn, _ct):
            continue
        try:
            conn.execute(
                f"ALTER TABLE {_ct} ADD COLUMN is_own INTEGER NOT NULL DEFAULT 0")
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e).lower():
                raise
    if _table_exists(conn, "comments"):
        conn.execute("CREATE INDEX IF NOT EXISTS idx_comments_own "
                     "ON comments(is_own, first_seen_at)")
    if _table_exists(conn, "fa_comments"):
        conn.execute("CREATE INDEX IF NOT EXISTS idx_fa_comments_own "
                     "ON fa_comments(is_own, first_seen_at)")
    if _table_exists(conn, "platform_comments"):
        conn.execute("CREATE INDEX IF NOT EXISTS idx_platform_comments_own "
                     "ON platform_comments(platform, is_own)")

    # Migration: beta-reader draft share tokens (gap-wave-5 §3). One table
    # mapping a url-safe token → story folder, gated by enabled + expiry. The
    # public /share/{token} route reads it. Additive, idempotent.
    from database import share_tokens as _share_tokens
    _share_tokens.ensure_share_tokens_table(conn)

    # Migration: commission archive flag (2.188). The commissions table shipped
    # in 2.187 without it, so existing DBs need the additive column; the schema
    # file carries it for fresh installs. Guarded on the table existing (legacy
    # DBs run migrations before the schema-load list).
    if _table_exists(conn, "commissions"):
        try:
            conn.execute("ALTER TABLE commissions ADD COLUMN archived INTEGER NOT NULL DEFAULT 0")
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e).lower():
                raise
        # Index created here (not in the schema file) so it runs AFTER the column
        # exists on both fresh installs and upgrades.
        conn.execute("CREATE INDEX IF NOT EXISTS idx_commissions_archived ON commissions(archived)")

    # Migration: fold Cross-Platform links into Collections (they are the same
    # idea — one piece across platforms). Adds a provenance column so the fold is
    # idempotent and reversible (the submission_links rows are NOT deleted), then
    # creates a Collection per not-yet-migrated link. See collections_queries.
    # Guarded on the collections table existing — legacy/partial DBs (e.g. the
    # legacy-migration tests) run _run_migrations before the collections schema
    # is applied, so this whole block is skipped there.
    # Native perceptual-hash store for pixel-based "same artwork?" suggestions
    # (Phase 4). Additive, standalone table — safe to create unconditionally.
    try:
        from database import image_hash as _image_hash
        _image_hash.ensure_table(conn)
    except Exception as e:
        logger.warning("image_hashes table ensure skipped: %s", e)

    _has_collections = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='collections'").fetchone()
    if _has_collections:
        try:
            conn.execute("ALTER TABLE collections ADD COLUMN source_link_id INTEGER")
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e).lower():
                raise
        try:
            from database import collections_queries as _cq
            _n = _cq.migrate_links_to_collections(conn)
            if _n:
                logger.info("Collections: migrated %d Cross-Platform link(s) into collections", _n)
        except Exception as e:  # never let a data migration block startup
            logger.warning("Collections link migration skipped: %s", e)

    # e621: trend the up/down vote split (previously only the net score was
    # snapshotted). Additive columns on e621_snapshots — guarded + idempotent.
    _has_e621_snap = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='e621_snapshots'").fetchone()
    if _has_e621_snap:
        _e621_snap_cols = {r[1] for r in conn.execute(
            "PRAGMA table_info(e621_snapshots)").fetchall()}
        for _col in ("up_score", "down_score"):
            if _col not in _e621_snap_cols:
                try:
                    conn.execute(
                        f"ALTER TABLE e621_snapshots ADD COLUMN {_col} INTEGER NOT NULL DEFAULT 0")
                except sqlite3.OperationalError as e:
                    if "duplicate column" not in str(e).lower():
                        raise

    # Migration: posting_queue.claimed_by — which instance is running this item.
    # The desktop and the server BOTH start the posting scheduler (main.py and
    # server.py), and today that is safe only because the two databases are
    # separate. The moment the queue is shared, two schedulers can pick up the
    # same row and post it twice to a live platform. claim_queue_item() makes the
    # claim a conditional UPDATE (see posting_queries); this column records who
    # won it, so a stuck 'processing' row can be attributed rather than guessed
    # at. Additive and nullable — rows claimed by older builds simply read NULL.
    # NB deliberately added here rather than in posting_schema.sql: schema load
    # runs BEFORE _run_migrations, so a column referenced from the schema file
    # would break the upgrade path while passing every fresh-install test.
    if "posting_queue" in tables:
        try:
            conn.execute("ALTER TABLE posting_queue ADD COLUMN claimed_by TEXT")
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e).lower():
                raise

    # Migration (3.7.0): the desktop↔server work handoff — mirroring Stage 2.
    # A job the server cannot run itself (FurAffinity blocks its datacenter IP)
    # is queued with requires='desktop' and then skipped by the server's own
    # scheduler forever, because the two installs have SEPARATE databases and
    # nothing ever carried the row across. One such row sat pending on prod from
    # 2026-07-24 to 2026-08-13. These columns let the desktop import a copy of
    # the server's row, run it through its ordinary scheduler, and report the
    # outcome back exactly once:
    #   origin_server      - which install the job came from (URL)
    #   origin_queue_id    - that install's queue_id, echoed back untouched
    #   origin_reported_at - set when the result has been delivered upstream
    # origin_queue_id is the one surrogate id allowed to cross, and only as an
    # opaque handle to the OTHER side's row: it is never interpreted locally and
    # never joined on. Everything with meaning travels as natural keys.
    # Reporting is a sweep over finished-but-unreported rows rather than an
    # inline call from the scheduler, so a crash between posting and reporting
    # retries instead of losing the result, and so the scheduler never blocks on
    # the network.
        for column, decl in (("origin_server", "TEXT"),
                             ("origin_queue_id", "INTEGER"),
                             ("origin_reported_at", "TEXT")):
            try:
                conn.execute(f"ALTER TABLE posting_queue ADD COLUMN {column} {decl}")
            except sqlite3.OperationalError as e:
                if "duplicate column" not in str(e).lower():
                    raise
        # Finding an unreported result is the sweeper's only query.
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_queue_origin_unreported "
            "ON posting_queue(origin_queue_id, origin_reported_at)"
        )

    # Migration (3.10.0): the live artist registry.
    #
    # 3.5.0 made `artist` a structured field on masterpiece.json and taught
    # `artist_credit` to render it per platform. What it never got was a place
    # to KEEP the artists: the 44-artist / 134-handle registry built by the
    # August lookup lived as a workspace JSON file, applied once as a migration
    # payload and then dormant. So filling in a missing artist meant hand-typing
    # the same FA/DA/e621/Bluesky handles that had already been researched and
    # verified, and nothing could warn about the artists carrying repost
    # prohibitions.
    #
    # Handles are their own table rather than a JSON blob because the whole
    # point is answering "where is this artist on <platform>" — a question a
    # blob cannot be indexed for.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS artists (
            artist_key  TEXT PRIMARY KEY,
            name        TEXT NOT NULL,
            aliases     TEXT DEFAULT '[]',
            flags       TEXT DEFAULT '[]',
            notes       TEXT DEFAULT '',
            created_at  TEXT DEFAULT (datetime('now')),
            updated_at  TEXT DEFAULT (datetime('now'))
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS artist_handles (
            artist_key  TEXT NOT NULL,
            platform    TEXT NOT NULL,
            handle      TEXT NOT NULL,
            confidence  TEXT DEFAULT '',
            PRIMARY KEY (artist_key, platform)
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_artist_handles_platform "
                 "ON artist_handles(platform, handle)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_artists_name ON artists(name)")

    # People (4.6.0, docs/specs/people_registry.md): a person row may BE one of
    # the operator's personas, and each handle carries whether it may be
    # @-mentioned on that site. Mention defaults OFF: a mention notifies, on
    # adult sites under handles a friend may keep apart on purpose — names are
    # free, links are consent. Guarded like every ADD COLUMN above.
    for _sql in ("ALTER TABLE artists ADD COLUMN persona_id INTEGER DEFAULT NULL",
                 "ALTER TABLE artist_handles ADD COLUMN mention INTEGER NOT NULL DEFAULT 0"):
        try:
            conn.execute(_sql)
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e).lower():
                raise

    # Migration (3.29.0): re-key the DeviantArt rows that stored the wrong id.
    # DA has two identifiers per deviation — the integer in every public URL and
    # the API's GUID. The integer is what PawPoller speaks everywhere: the
    # poller writes it to `da_submissions`, image hashes and Masterpiece members
    # key on it, and `clients/da/client.py` says so in its header. The poster
    # stored the GUID (fixed at source in 3.29.0), so anything published to DA
    # through PawPoller wrote an id that joined to nothing — the piece then kept
    # offering its own upload back as "is this the same image?".
    #
    # Repairable without a network call because the integer is in the stored
    # URL. Runs before the tombstone triggers below so a first-time repair does
    # not enqueue mirror deletes for rows it is merely re-keying.
    try:
        _da_rekey(conn)
    except Exception as e:  # a data repair must never block startup
        logger.warning("DA id re-key skipped: %s", e)

    # Migration (3.8.0): the shared-table delete outbox — mirroring Stage 3.
    # The upward sync is a natural-key upsert, which is additive by nature and
    # therefore cannot express "this row is gone". For four tables a delete IS
    # the user's intent (un-ignoring a submission, un-handling an inbox comment,
    # unlinking a Masterpiece member, removing a Collection member), so an
    # AFTER DELETE trigger records the natural key and the push replays it.
    # Triggers rather than call-site bookkeeping because ON DELETE CASCADE on
    # collection_members runs no Python at all, and raw SQL bypasses any helper.
    # Runs last: the triggers are only created for tables that already exist,
    # so every migration above has had its chance to create them first.
    try:
        from mirror import tombstones as _tombstones
        _tombstones.ensure_tombstones(conn)
    except Exception as e:  # never let the outbox block startup
        logger.warning("Mirror tombstone outbox ensure skipped: %s", e)

    conn.commit()


_DA_URL_ID_RE = re.compile(r"-(\d+)(?:[/?#].*)?$")


def _da_int_id(url: str) -> str:
    """The integer deviation id at the end of a DeviantArt URL, or ""."""
    m = _DA_URL_ID_RE.search((url or "").rstrip("/"))
    return m.group(1) if m else ""


def _da_rekey(conn: sqlite3.Connection) -> None:
    """Replace GUID DeviantArt ids with the integer the rest of the app uses.

    Idempotent by construction: it only looks at rows whose id is not all
    digits, and every row it fixes becomes all digits. After the first run there
    is nothing left to match, so this is a no-op on every later startup.

    Three tables carry the id, and each needs a different source for the
    integer:

    - ``publications`` stores ``external_url`` alongside, and the integer is the
      last path segment of it. Direct.
    - ``image_hashes`` has no URL, but its ``(platform, submission_id)`` matches
      a publication we can look through.
    - ``masterpiece_members`` has neither. Resolved by the DA publication of the
      same Masterpiece — preferring one that already holds an integer, then
      parsing the URL of the one holding this exact GUID.

    Where the correct row already exists (a piece phash-linked before it was
    published, so it has BOTH a good integer member and a stray GUID one) the
    GUID row is **deleted** rather than updated — an UPDATE would collide with
    the primary key, and the delete is the honest statement: that member was
    never real.

    Anything it cannot resolve is left alone and logged. A wrong id is bad; a
    guessed one is worse.
    """
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    if "publications" not in tables:
        return

    # ── publications ────────────────────────────────────────────────
    fixed_pubs = 0
    url_by_guid: dict[str, str] = {}
    int_by_story: dict[str, str] = {}
    rows = conn.execute(
        "SELECT pub_id, story_name, external_id, external_url FROM publications "
        "WHERE platform = 'da' AND external_id != ''").fetchall()
    for r in rows:
        eid = str(r["external_id"])
        if eid.isdigit():
            int_by_story.setdefault(r["story_name"], eid)
            continue
        num = _da_int_id(r["external_url"])
        url_by_guid[eid] = num
        if not num:
            logger.warning("DA re-key: no integer id in URL for publication %s (%r)",
                           r["pub_id"], r["external_url"])
            continue
        conn.execute("UPDATE publications SET external_id = ? WHERE pub_id = ?",
                     (num, r["pub_id"]))
        int_by_story.setdefault(r["story_name"], num)
        fixed_pubs += 1

    # ── image_hashes ────────────────────────────────────────────────
    fixed_hashes = deleted_hashes = 0
    if "image_hashes" in tables:
        for r in conn.execute(
                "SELECT submission_id FROM image_hashes WHERE platform = 'da'").fetchall():
            sid = str(r["submission_id"])
            if sid.isdigit():
                continue
            num = url_by_guid.get(sid, "")
            if not num:
                continue
            exists = conn.execute(
                "SELECT 1 FROM image_hashes WHERE platform = 'da' AND submission_id = ?",
                (num,)).fetchone()
            if exists:
                conn.execute(
                    "DELETE FROM image_hashes WHERE platform = 'da' AND submission_id = ?",
                    (sid,))
                deleted_hashes += 1
            else:
                conn.execute(
                    "UPDATE image_hashes SET submission_id = ? "
                    "WHERE platform = 'da' AND submission_id = ?", (num, sid))
                fixed_hashes += 1

    # ── masterpiece_members ─────────────────────────────────────────
    fixed_members = deleted_members = 0
    if "masterpiece_members" in tables:
        for r in conn.execute(
                "SELECT masterpiece_name, submission_id FROM masterpiece_members "
                "WHERE platform = 'da'").fetchall():
            sid = str(r["submission_id"])
            if sid.isdigit():
                continue
            name = r["masterpiece_name"]
            num = int_by_story.get(name) or url_by_guid.get(sid, "")
            if not num:
                logger.warning("DA re-key: cannot resolve member %s/%s — left as is",
                               name, sid)
                continue
            exists = conn.execute(
                "SELECT 1 FROM masterpiece_members WHERE masterpiece_name = ? "
                "AND platform = 'da' AND submission_id = ?", (name, num)).fetchone()
            if exists:
                conn.execute(
                    "DELETE FROM masterpiece_members WHERE masterpiece_name = ? "
                    "AND platform = 'da' AND submission_id = ?", (name, sid))
                deleted_members += 1
            else:
                conn.execute(
                    "UPDATE masterpiece_members SET submission_id = ? "
                    "WHERE masterpiece_name = ? AND platform = 'da' AND submission_id = ?",
                    (num, name, sid))
                fixed_members += 1

    if fixed_pubs or fixed_members or deleted_members or fixed_hashes or deleted_hashes:
        logger.info(
            "DA id re-key: %d publications, %d members re-keyed (%d duplicate members "
            "dropped), %d hashes re-keyed (%d dropped)",
            fixed_pubs, fixed_members, deleted_members, fixed_hashes, deleted_hashes)
