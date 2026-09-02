"""Migration test: a legacy DB gains the content_type discriminator.

Exercises the additive content_type column on publications/posting_queue/
posting_log (in _run_migrations) and the publications UNIQUE rebuild that folds
content_type into the key (_rebuild_publications_content_type, in
_run_table_rebuilds). Built on a pre-multiaccount publications shape so the run
also covers the defensive branch where the account_id rebuild drops the freshly
added column before the content_type rebuild re-adds it.
"""

import json
import sqlite3

import pytest

import config

# Full pre-multiaccount schema (no account_id, no content_type) — mirrors the
# multiaccount migration test's fixture so the run reaches both rebuilds (the
# account_id rebuild AND the content_type fold) cleanly. Includes every table
# _run_migrations references unconditionally.
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
CREATE TABLE poll_log (id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT, status TEXT);
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
CREATE TABLE sf_submissions (submission_id TEXT PRIMARY KEY, title TEXT, views INTEGER DEFAULT 0,
    favorites_count INTEGER DEFAULT 0, comments_count INTEGER DEFAULT 0);
CREATE TABLE sf_snapshots (id INTEGER PRIMARY KEY AUTOINCREMENT, submission_id TEXT, polled_at TEXT,
    views INTEGER, favorites_count INTEGER, comments_count INTEGER);
CREATE TABLE sf_watchers (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL,
    first_seen_at TEXT, UNIQUE(username));
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
    dbfile = tmp_path / "legacy_ct.db"
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(json.dumps({"username": "kit", "password": "pw"}), encoding="utf-8")
    monkeypatch.setattr(config, "DB_PATH", dbfile)
    monkeypatch.setattr(config, "SETTINGS_PATH", settings_file)

    conn = sqlite3.connect(str(dbfile))
    conn.row_factory = sqlite3.Row
    conn.executescript(LEGACY_SCHEMA)
    conn.execute("INSERT INTO submissions (submission_id, title) VALUES (1, 'Legacy')")
    conn.execute("INSERT INTO publications (story_name, chapter_index, platform, external_id, status)"
                 " VALUES ('My_Story', 0, 'ib', '555', 'posted')")
    conn.execute("INSERT INTO posting_queue (story_name, chapter_index, platform) VALUES ('My_Story', 0, 'ib')")
    conn.execute("INSERT INTO posting_log (story_name, chapter_index, platform, action, status)"
                 " VALUES ('My_Story', 0, 'ib', 'post', 'success')")
    conn.commit()
    yield dbfile, conn
    conn.close()


def _migrate(conn):
    from database.db import _run_migrations, _run_table_rebuilds
    _run_migrations(conn)
    conn.commit()
    _run_table_rebuilds()


class TestContentTypeMigration:
    def test_content_type_columns_added(self, legacy_db):
        dbfile, conn = legacy_db
        _migrate(conn)
        c = sqlite3.connect(str(dbfile)); c.row_factory = sqlite3.Row
        try:
            for table in ("publications", "posting_queue", "posting_log"):
                cols = {r[1] for r in c.execute(f"PRAGMA table_info({table})").fetchall()}
                assert "content_type" in cols, table
        finally:
            c.close()

    def test_unique_folds_in_content_type_and_account_id(self, legacy_db):
        dbfile, conn = legacy_db
        _migrate(conn)
        c = sqlite3.connect(str(dbfile)); c.row_factory = sqlite3.Row
        try:
            sql = c.execute("SELECT sql FROM sqlite_master WHERE name='publications'").fetchone()["sql"]
            assert "UNIQUE(content_type" in sql
            assert "account_id" in sql  # account_id rebuild also applied
            # Legacy row survived with pub_id preserved + backfilled to 'story'.
            row = c.execute("SELECT pub_id, content_type FROM publications WHERE story_name='My_Story'").fetchone()
            assert row["content_type"] == "story"
        finally:
            c.close()

    def test_existing_rows_backfilled_to_story(self, legacy_db):
        dbfile, conn = legacy_db
        _migrate(conn)
        c = sqlite3.connect(str(dbfile)); c.row_factory = sqlite3.Row
        try:
            for table in ("publications", "posting_queue", "posting_log"):
                rows = c.execute(f"SELECT content_type FROM {table}").fetchall()
                assert rows and all(r["content_type"] == "story" for r in rows), table
        finally:
            c.close()

    def test_story_and_artwork_coexist(self, legacy_db):
        dbfile, conn = legacy_db
        _migrate(conn)
        c = sqlite3.connect(str(dbfile)); c.row_factory = sqlite3.Row
        try:
            acct = c.execute("SELECT account_id FROM publications WHERE story_name='My_Story'").fetchone()["account_id"]
            # An artwork with the SAME (name, chapter, platform, account) coexists.
            c.execute("INSERT INTO publications (content_type, story_name, chapter_index, platform, account_id, external_id, status)"
                      " VALUES ('artwork', 'My_Story', 0, 'ib', ?, '999', 'posted')", (acct,))
            c.commit()
            rows = c.execute("SELECT content_type FROM publications WHERE story_name='My_Story' ORDER BY content_type").fetchall()
            assert [r["content_type"] for r in rows] == ["artwork", "story"]
            # A duplicate of the SAME content_type still violates UNIQUE.
            with pytest.raises(sqlite3.IntegrityError):
                c.execute("INSERT INTO publications (content_type, story_name, chapter_index, platform, account_id, external_id, status)"
                          " VALUES ('story', 'My_Story', 0, 'ib', ?, '000', 'posted')", (acct,))
                c.commit()
        finally:
            c.close()

    def test_idempotent(self, legacy_db):
        dbfile, conn = legacy_db
        _migrate(conn)
        # Re-running must not raise or duplicate the legacy row.
        c = sqlite3.connect(str(dbfile)); c.row_factory = sqlite3.Row
        try:
            from database.db import _run_migrations, _run_table_rebuilds
            _run_migrations(c); c.commit(); _run_table_rebuilds()
            cnt = c.execute("SELECT COUNT(*) AS n FROM publications WHERE story_name='My_Story'").fetchone()["n"]
            assert cnt == 1
            sql = c.execute("SELECT sql FROM sqlite_master WHERE name='publications'").fetchone()["sql"]
            assert sql.count("UNIQUE(content_type") == 1
        finally:
            c.close()
