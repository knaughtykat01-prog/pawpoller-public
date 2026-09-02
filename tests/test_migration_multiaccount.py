"""Migration tests: a legacy single-account DB upgrades to multi-account.

Exercises the riskiest part of the multi-account work — the additive account_id
columns + backfill (in _run_migrations) and the constraint-changing rebuilds of
session_cache / watchers / publications (in _run_table_rebuilds), against a DB
built in the OLD single-account shape with real legacy rows.
"""

import json
import sqlite3

import pytest

import config

# The pre-multi-account schema for the tables the migration touches (subset that
# matters for backfill + the auxiliary poll_log tables the migration ALTERs).
LEGACY_SCHEMA = """
CREATE TABLE submissions (
    submission_id INTEGER PRIMARY KEY, title TEXT, username TEXT,
    views INTEGER DEFAULT 0, favorites_count INTEGER DEFAULT 0,
    comments_count INTEGER DEFAULT 0, updated_at TEXT
);
CREATE TABLE snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT, submission_id INTEGER NOT NULL,
    polled_at TEXT, views INTEGER, favorites_count INTEGER, comments_count INTEGER
);
CREATE TABLE comments (
    comment_id INTEGER PRIMARY KEY, submission_id INTEGER NOT NULL,
    username TEXT, comment_text TEXT, first_seen_at TEXT
);
CREATE TABLE poll_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT, status TEXT
);
CREATE TABLE fa_poll_log (id INTEGER PRIMARY KEY AUTOINCREMENT, status TEXT);
CREATE TABLE sf_poll_log (id INTEGER PRIMARY KEY AUTOINCREMENT, status TEXT);
CREATE TABLE faving_users (
    id INTEGER PRIMARY KEY AUTOINCREMENT, submission_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL, username TEXT, first_seen_at TEXT,
    UNIQUE(submission_id, user_id)
);
CREATE TABLE watchers (
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL DEFAULT 0,
    username TEXT NOT NULL, first_seen_at TEXT, UNIQUE(username)
);
CREATE TABLE session_cache (
    id INTEGER PRIMARY KEY CHECK (id = 1), sid TEXT NOT NULL,
    username TEXT NOT NULL, user_id INTEGER NOT NULL DEFAULT 0, created_at TEXT
);
CREATE TABLE publications (
    pub_id INTEGER PRIMARY KEY AUTOINCREMENT, story_name TEXT NOT NULL,
    chapter_index INTEGER DEFAULT 0, chapter_title TEXT DEFAULT '',
    platform TEXT NOT NULL, external_id TEXT NOT NULL DEFAULT '',
    external_url TEXT DEFAULT '', format_file TEXT DEFAULT '',
    file_hash TEXT DEFAULT '', tags_used TEXT DEFAULT '[]', title_used TEXT DEFAULT '',
    description_used TEXT DEFAULT '', rating_used TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'draft', first_posted_at TEXT, last_updated_at TEXT,
    update_count INTEGER DEFAULT 0, last_error TEXT, created_at TEXT, word_count INTEGER DEFAULT 0,
    UNIQUE(story_name, chapter_index, platform)
);
CREATE TABLE posting_queue (
    queue_id INTEGER PRIMARY KEY AUTOINCREMENT, story_name TEXT, chapter_index INTEGER,
    platform TEXT, pub_id INTEGER REFERENCES publications(pub_id)
);
CREATE TABLE posting_log (
    log_id INTEGER PRIMARY KEY AUTOINCREMENT, pub_id INTEGER REFERENCES publications(pub_id),
    platform TEXT, story_name TEXT, chapter_index INTEGER, action TEXT, status TEXT
);
-- FurAffinity legacy tables
CREATE TABLE fa_submissions (
    submission_id INTEGER PRIMARY KEY, title TEXT, username TEXT,
    views INTEGER DEFAULT 0, favorites_count INTEGER DEFAULT 0,
    comments_count INTEGER DEFAULT 0, updated_at TEXT
);
CREATE TABLE fa_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT, submission_id INTEGER NOT NULL,
    polled_at TEXT, views INTEGER, favorites_count INTEGER, comments_count INTEGER
);
CREATE TABLE fa_comments (
    comment_id TEXT PRIMARY KEY, submission_id INTEGER NOT NULL,
    username TEXT, comment_text TEXT, first_seen_at TEXT
);
CREATE TABLE fa_profile_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT, polled_at TEXT, pageviews INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE fa_watchers (
    id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL,
    first_seen_at TEXT, confirmed INTEGER NOT NULL DEFAULT 0,
    last_seen_at TEXT, is_spam INTEGER NOT NULL DEFAULT 0,
    notified INTEGER NOT NULL DEFAULT 0, UNIQUE(username)
);
-- Weasyl legacy tables (no watcher/kudos/session)
CREATE TABLE ws_submissions (
    submission_id INTEGER PRIMARY KEY, title TEXT, username TEXT,
    views INTEGER DEFAULT 0, favorites_count INTEGER DEFAULT 0,
    comments_count INTEGER DEFAULT 0, updated_at TEXT
);
CREATE TABLE ws_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT, submission_id INTEGER NOT NULL,
    polled_at TEXT, views INTEGER, favorites_count INTEGER, comments_count INTEGER
);
CREATE TABLE ws_poll_log (id INTEGER PRIMARY KEY AUTOINCREMENT, status TEXT);
-- SoFurry (watcher table, UNIQUE(username) — needs rebuild)
CREATE TABLE sf_submissions (submission_id TEXT PRIMARY KEY, title TEXT, views INTEGER DEFAULT 0,
    favorites_count INTEGER DEFAULT 0, comments_count INTEGER DEFAULT 0);
CREATE TABLE sf_snapshots (id INTEGER PRIMARY KEY AUTOINCREMENT, submission_id TEXT, polled_at TEXT,
    views INTEGER, favorites_count INTEGER, comments_count INTEGER);
-- (sf_poll_log already declared above for the _run_migrations ALTERs)
CREATE TABLE sf_watchers (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL,
    first_seen_at TEXT, UNIQUE(username));
-- SquidgeWorld (kudos table, UNIQUE(submission_id, username) — additive account_id)
CREATE TABLE sqw_submissions (submission_id INTEGER PRIMARY KEY, title TEXT, views INTEGER DEFAULT 0,
    favorites_count INTEGER DEFAULT 0, comments_count INTEGER DEFAULT 0, bookmarks_count INTEGER DEFAULT 0);
CREATE TABLE sqw_snapshots (id INTEGER PRIMARY KEY AUTOINCREMENT, submission_id INTEGER, polled_at TEXT,
    views INTEGER, favorites_count INTEGER, comments_count INTEGER, bookmarks_count INTEGER);
CREATE TABLE sqw_poll_log (id INTEGER PRIMARY KEY AUTOINCREMENT, status TEXT);
CREATE TABLE sqw_kudos_users (id INTEGER PRIMARY KEY AUTOINCREMENT, submission_id INTEGER NOT NULL,
    username TEXT NOT NULL, first_seen_at TEXT DEFAULT (datetime('now')), UNIQUE(submission_id, username));
"""


@pytest.fixture
def legacy_db(tmp_path, monkeypatch):
    """A DB in the pre-multi-account shape with legacy rows + IB credentials."""
    dbfile = tmp_path / "legacy.db"
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(json.dumps({
        "username": "kit", "password": "pw",
        "fa_username": "fox", "fa_cookie_a": "aaa", "fa_cookie_b": "bbb",
        "ws_api_key": "wskey",
        "sf_api_token": "sftok",
        "sqw_username": "sqwu", "sqw_password": "sqwp",
    }), encoding="utf-8")
    monkeypatch.setattr(config, "DB_PATH", dbfile)
    monkeypatch.setattr(config, "SETTINGS_PATH", settings_file)

    conn = sqlite3.connect(str(dbfile))
    conn.row_factory = sqlite3.Row
    conn.executescript(LEGACY_SCHEMA)
    conn.execute("INSERT INTO submissions (submission_id, title) VALUES (1, 'Legacy')")
    conn.execute("INSERT INTO snapshots (submission_id, polled_at, views, favorites_count, comments_count)"
                 " VALUES (1, datetime('now'), 10, 2, 0)")
    conn.execute("INSERT INTO faving_users (submission_id, user_id, username, first_seen_at)"
                 " VALUES (1, 99, 'fan', datetime('now'))")
    conn.execute("INSERT INTO watchers (user_id, username, first_seen_at) VALUES (0, 'watcher1', datetime('now'))")
    conn.execute("INSERT INTO session_cache (id, sid, username, user_id, created_at)"
                 " VALUES (1, 'OLDSID', 'kit', 42, datetime('now'))")
    conn.execute("INSERT INTO publications (story_name, chapter_index, platform, external_id, status)"
                 " VALUES ('Story', 1, 'ib', '555', 'posted')")
    # FA legacy rows
    conn.execute("INSERT INTO fa_submissions (submission_id, title) VALUES (700, 'FA Legacy')")
    conn.execute("INSERT INTO fa_snapshots (submission_id, polled_at, views, favorites_count, comments_count)"
                 " VALUES (700, datetime('now'), 5, 1, 0)")
    conn.execute("INSERT INTO fa_watchers (username, first_seen_at, confirmed, last_seen_at)"
                 " VALUES ('fafan', datetime('now'), 1, datetime('now'))")
    conn.execute("INSERT INTO fa_profile_stats (polled_at, pageviews) VALUES (datetime('now'), 123)")
    # Weasyl legacy rows
    conn.execute("INSERT INTO ws_submissions (submission_id, title, views) VALUES (900, 'WS Legacy', 42)")
    conn.execute("INSERT INTO ws_snapshots (submission_id, polled_at, views, favorites_count, comments_count)"
                 " VALUES (900, datetime('now'), 42, 3, 1)")
    # SoFurry + SquidgeWorld legacy rows
    conn.execute("INSERT INTO sf_submissions (submission_id, title, views) VALUES ('s1', 'SF', 9)")
    conn.execute("INSERT INTO sf_watchers (username, first_seen_at) VALUES ('sffollower', datetime('now'))")
    conn.execute("INSERT INTO sqw_submissions (submission_id, title, views) VALUES (12, 'SQW', 4)")
    conn.execute("INSERT INTO sqw_kudos_users (submission_id, username, first_seen_at) VALUES (12, 'kudosfan', datetime('now'))")
    conn.commit()
    yield dbfile, conn
    conn.close()


def _migrate(dbfile, conn):
    from database.db import _run_migrations, _run_table_rebuilds
    _run_migrations(conn)
    conn.commit()
    _run_table_rebuilds()


class TestLegacyMigration:
    def test_default_account_seeded(self, legacy_db):
        dbfile, conn = legacy_db
        _migrate(dbfile, conn)
        from database import accounts
        c = sqlite3.connect(str(dbfile)); c.row_factory = sqlite3.Row
        try:
            assert accounts.get_default_account_id(c, "ib") is not None
        finally:
            c.close()

    def test_analytics_rows_backfilled(self, legacy_db):
        dbfile, conn = legacy_db
        _migrate(dbfile, conn)
        from database import accounts
        c = sqlite3.connect(str(dbfile)); c.row_factory = sqlite3.Row
        try:
            ib = accounts.get_default_account_id(c, "ib")
            for table in ("submissions", "snapshots", "faving_users"):
                rows = c.execute(f"SELECT account_id FROM {table}").fetchall()
                assert rows and all(r["account_id"] == ib for r in rows), table
        finally:
            c.close()

    def test_session_cache_rebuilt_per_account(self, legacy_db):
        dbfile, conn = legacy_db
        _migrate(dbfile, conn)
        from database import accounts
        c = sqlite3.connect(str(dbfile)); c.row_factory = sqlite3.Row
        try:
            cols = {r[1] for r in c.execute("PRAGMA table_info(session_cache)").fetchall()}
            assert "account_id" in cols and "id" not in cols
            ib = accounts.get_default_account_id(c, "ib")
            row = c.execute("SELECT * FROM session_cache").fetchone()
            assert row["account_id"] == ib
            assert row["sid"] == "OLDSID"
        finally:
            c.close()

    def test_watchers_unique_now_includes_account(self, legacy_db):
        dbfile, conn = legacy_db
        _migrate(dbfile, conn)
        from database import accounts
        c = sqlite3.connect(str(dbfile)); c.row_factory = sqlite3.Row
        try:
            sql = c.execute("SELECT sql FROM sqlite_master WHERE name='watchers'").fetchone()["sql"]
            assert "account_id" in sql and "username" in sql
            ib = accounts.get_default_account_id(c, "ib")
            assert c.execute("SELECT account_id FROM watchers WHERE username='watcher1'").fetchone()["account_id"] == ib
            # The same username can now exist for a different account.
            c.execute("INSERT INTO watchers (account_id, username, first_seen_at) VALUES (?, 'watcher1', datetime('now'))",
                      (ib + 1000,))
            c.commit()
        finally:
            c.close()

    def test_publications_allows_same_chapter_per_account(self, legacy_db):
        dbfile, conn = legacy_db
        _migrate(dbfile, conn)
        from database import accounts
        c = sqlite3.connect(str(dbfile)); c.row_factory = sqlite3.Row
        try:
            cols = {r[1] for r in c.execute("PRAGMA table_info(publications)").fetchall()}
            assert "account_id" in cols
            ib = accounts.get_default_account_id(c, "ib")
            existing = c.execute("SELECT account_id, pub_id FROM publications WHERE story_name='Story'").fetchone()
            assert existing["account_id"] == ib
            # Same (story, chapter, platform) for a different account is now allowed.
            c.execute("INSERT INTO publications (story_name, chapter_index, platform, account_id, external_id, status)"
                      " VALUES ('Story', 1, 'ib', ?, '777', 'posted')", (ib + 1000,))
            c.commit()
            # ...but a duplicate for the SAME account still violates UNIQUE.
            with pytest.raises(sqlite3.IntegrityError):
                c.execute("INSERT INTO publications (story_name, chapter_index, platform, account_id, external_id, status)"
                          " VALUES ('Story', 1, 'ib', ?, '888', 'posted')", (ib,))
                c.commit()
        finally:
            c.close()

    def test_fa_default_account_and_backfill(self, legacy_db):
        dbfile, conn = legacy_db
        _migrate(dbfile, conn)
        from database import accounts
        c = sqlite3.connect(str(dbfile)); c.row_factory = sqlite3.Row
        try:
            fa = accounts.get_default_account_id(c, "fa")
            assert fa is not None
            # FA default account is a DIFFERENT id from the IB default.
            assert fa != accounts.get_default_account_id(c, "ib")
            for table in ("fa_submissions", "fa_snapshots", "fa_profile_stats"):
                rows = c.execute(f"SELECT account_id FROM {table}").fetchall()
                assert rows and all(r["account_id"] == fa for r in rows), table
        finally:
            c.close()

    def test_fa_watchers_rebuilt_per_account(self, legacy_db):
        dbfile, conn = legacy_db
        _migrate(dbfile, conn)
        from database import accounts
        c = sqlite3.connect(str(dbfile)); c.row_factory = sqlite3.Row
        try:
            sql = c.execute("SELECT sql FROM sqlite_master WHERE name='fa_watchers'").fetchone()["sql"]
            assert "account_id" in sql
            # Spam-protection columns must survive the rebuild.
            cols = {r[1] for r in c.execute("PRAGMA table_info(fa_watchers)").fetchall()}
            assert {"confirmed", "last_seen_at", "is_spam", "notified"} <= cols
            fa = accounts.get_default_account_id(c, "fa")
            row = c.execute("SELECT * FROM fa_watchers WHERE username='fafan'").fetchone()
            assert row["account_id"] == fa and row["confirmed"] == 1
        finally:
            c.close()

    def test_ws_default_account_and_backfill(self, legacy_db):
        dbfile, conn = legacy_db
        _migrate(dbfile, conn)
        from database import accounts
        c = sqlite3.connect(str(dbfile)); c.row_factory = sqlite3.Row
        try:
            ws = accounts.get_default_account_id(c, "ws")
            assert ws is not None
            for table in ("ws_submissions", "ws_snapshots"):
                rows = c.execute(f"SELECT account_id FROM {table}").fetchall()
                assert rows and all(r["account_id"] == ws for r in rows), table
        finally:
            c.close()

    def test_sf_watchers_rebuilt_and_sqw_kudos_backfilled(self, legacy_db):
        dbfile, conn = legacy_db
        _migrate(dbfile, conn)
        from database import accounts
        c = sqlite3.connect(str(dbfile)); c.row_factory = sqlite3.Row
        try:
            # sf_watchers rebuilt to UNIQUE(account_id, username), backfilled.
            sf = accounts.get_default_account_id(c, "sf")
            sql = c.execute("SELECT sql FROM sqlite_master WHERE name='sf_watchers'").fetchone()["sql"]
            assert "account_id" in sql
            assert c.execute("SELECT account_id FROM sf_watchers WHERE username='sffollower'").fetchone()["account_id"] == sf
            # sqw_kudos_users gained account_id (additive), backfilled.
            sqw = accounts.get_default_account_id(c, "sqw")
            kcols = {r[1] for r in c.execute("PRAGMA table_info(sqw_kudos_users)").fetchall()}
            assert "account_id" in kcols
            assert c.execute("SELECT account_id FROM sqw_kudos_users WHERE username='kudosfan'").fetchone()["account_id"] == sqw
        finally:
            c.close()

    def test_migration_is_idempotent(self, legacy_db):
        dbfile, conn = legacy_db
        _migrate(dbfile, conn)
        # Re-running must not raise or duplicate.
        c = sqlite3.connect(str(dbfile)); c.row_factory = sqlite3.Row
        try:
            from database.db import _run_migrations, _run_table_rebuilds
            _run_migrations(c)
            c.commit()
            _run_table_rebuilds()
            from database import accounts
            assert accounts.count_accounts(c, "ib") == 1
        finally:
            c.close()
