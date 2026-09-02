"""Shared fixtures for posting module tests."""

import json
import os
import sqlite3
import tempfile
from pathlib import Path

import pytest

# Patch config before any PawPoller imports so DB/settings point to temp locations
_tmpdir = tempfile.mkdtemp(prefix="pawpoller_test_")
os.environ["PAWPOLLER_TEST_MODE"] = "1"

import config
config.DB_PATH = Path(_tmpdir) / "test.db"
config.SETTINGS_PATH = Path(_tmpdir) / "test_settings.json"
# The vault is always-on: save_settings() writes VAULT_PATH on every save,
# so it MUST be redirected or the suite would clobber the real vault.
config.VAULT_PATH = Path(_tmpdir) / "test_settings.vault.json"
# Deterministic operator key for the whole suite — keeps _get_vault_key()
# away from the real OS keyring / dotfile. Tests that exercise key
# resolution explicitly monkeypatch these env vars themselves.
if not os.environ.get("PAWPOLLER_VAULT_KEY"):
    from cryptography.fernet import Fernet
    os.environ["PAWPOLLER_VAULT_KEY"] = Fernet.generate_key().decode()
# Write minimal settings
config.SETTINGS_PATH.write_text("{}", encoding="utf-8")


@pytest.fixture(scope="session")
def _db_template(tmp_path_factory):
    """The initialised schema, built ONCE, as bytes.

    `init_db()` reads ~20 schema files and executes every CREATE plus the
    migration chain — about 0.9s. Doing that per test was the entire cost of
    the suite: 992 tests × ~0.9s ≈ 15 of its ~17 minutes, paid even by tests
    that never open a database. The DDL is identical every time, so it is
    built once here and each test gets a byte copy instead (~1ms).

    Two details make the copy safe. Connections run in **WAL** mode, so the
    checkpoint below folds the write-ahead log back into the main file —
    without it a single-file copy would silently miss committed schema. And a
    fresh init leaves exactly one row (a `pp_meta` migration marker whose
    timestamp is informational), so a cached template is semantically
    identical to a fresh init rather than merely close.
    """
    d = tmp_path_factory.mktemp("db_template")
    saved = (config.DB_PATH, config.SETTINGS_PATH, config.VAULT_PATH)
    config.DB_PATH = d / "template.db"
    config.SETTINGS_PATH = d / "settings.json"
    config.VAULT_PATH = d / "vault.json"
    config.SETTINGS_PATH.write_text("{}", encoding="utf-8")
    try:
        from database.db import get_connection, init_db
        init_db()
        conn = get_connection()
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()
        return config.DB_PATH.read_bytes()
    finally:
        config.DB_PATH, config.SETTINGS_PATH, config.VAULT_PATH = saved


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch, _db_template):
    """Give every test its own fresh, fully-initialised database + settings file.

    The suite used to share ONE temp DB across all tests, isolating only by
    per-test `DELETE`s (which swallowed OperationalErrors, so a stale row or a
    leaked WAL lock bled into later tests → the intermittent assertion failures
    in test_personas / test_scope_bsky). A shared WAL file also meant any leaked
    connection stalled other tests for up to `busy_timeout` (30s) — the whole
    suite took ~15 min. Pointing `config.DB_PATH` at a per-test file (get_connection
    reads it fresh each call) removes both the bleed and the contention. `monkeypatch`
    auto-reverts after each test. Runs before other DB fixtures (autouse, deps only
    on builtins), so their init_db()/wipes operate on this clean file.

    Isolation is unchanged — still a private file per test. Only the *cost* of
    producing it changed: the schema is copied from `_db_template` rather than
    rebuilt. A test that calls `init_db()` itself still works, because it is
    idempotent (CREATE TABLE IF NOT EXISTS) and a no-op against this file.
    """
    db_path = tmp_path / "test.db"
    db_path.write_bytes(_db_template)
    monkeypatch.setattr(config, "DB_PATH", db_path)
    monkeypatch.setattr(config, "SETTINGS_PATH", tmp_path / "test_settings.json")
    monkeypatch.setattr(config, "VAULT_PATH", tmp_path / "test_settings.vault.json")
    config.SETTINGS_PATH.write_text("{}", encoding="utf-8")
    yield


@pytest.fixture(autouse=False)
def db_conn():
    """Fresh database connection with posting tables wiped between tests."""
    from database.db import init_db, get_connection
    init_db()
    conn = get_connection()
    # Wipe ALL posting tables for test isolation (order matters for FK)
    conn.execute("DELETE FROM posting_log")
    conn.execute("DELETE FROM posting_queue")
    conn.execute("DELETE FROM publications")
    conn.commit()
    yield conn
    conn.close()

    # Also wipe with a separate connection to ensure cross-test isolation
    conn2 = get_connection()
    conn2.execute("DELETE FROM posting_log")
    conn2.execute("DELETE FROM posting_queue")
    conn2.execute("DELETE FROM publications")
    conn2.commit()
    conn2.close()


@pytest.fixture
def story_archive(tmp_path):
    """Create a minimal story archive structure for testing."""
    story_dir = tmp_path / "Test_Story"
    story_dir.mkdir()

    # Markdown/MASTER.md
    md_dir = story_dir / "Markdown"
    md_dir.mkdir()
    (md_dir / "MASTER.md").write_text(
        "# Test Story\n\nOnce upon a time...\n\n---\n\n# Chapter 2: The End\n\nThe end.\n",
        encoding="utf-8",
    )

    # Chapters structure
    chapters_dir = story_dir / "Chapters"
    chapters_dir.mkdir()
    bb_dir = chapters_dir / "BBCode"
    bb_dir.mkdir()
    (bb_dir / "Chapter_1_Beginning.txt").write_text(
        "[center][b]Test Story[/b][/center]\n\nOnce upon a time...\n",
        encoding="utf-8",
    )
    (bb_dir / "Chapter_2_The_End.txt").write_text(
        "[center][b]Chapter 2: The End[/b][/center]\n\nThe end.\n",
        encoding="utf-8",
    )

    sf_dir = chapters_dir / "SoFurry_HTML"
    sf_dir.mkdir()
    (sf_dir / "Chapter_1_Beginning.html").write_text(
        "<p>Once upon a time...</p>",
        encoding="utf-8",
    )

    # split_manifest.json
    manifest = {
        "story": "Test Story",
        "author": "TestAuthor",
        "total_chapters": 2,
        "total_words": 100,
        "split_date": "2026-04-01",
        "chapters": [
            {
                "index": 1,
                "title": "Beginning",
                "filename": "Chapter_1_Beginning",
                "word_count": 60,
                "files": {
                    "markdown": "Markdown/Chapter_1_Beginning.md",
                    "bbcode": "BBCode/Chapter_1_Beginning.txt",
                    "sofurry_html": "SoFurry_HTML/Chapter_1_Beginning.html",
                },
            },
            {
                "index": 2,
                "title": "The End",
                "filename": "Chapter_2_The_End",
                "word_count": 40,
                "files": {
                    "markdown": "Markdown/Chapter_2_The_End.md",
                    "bbcode": "BBCode/Chapter_2_The_End.txt",
                },
            },
        ],
    }
    (chapters_dir / "split_manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )

    # Tags/tags_upload.txt
    tags_dir = story_dir / "Tags"
    tags_dir.mkdir()
    (tags_dir / "tags_upload.txt").write_text(
        """TEST STORY - Master Upload File
=====================================
Total Parts: 2 | Total Words: ~100

STORY DESCRIPTION:
A test story for unit testing.

=============================================
PART 1 OF 2: "Beginning" (~60 words)
=============================================

DESCRIPTION:
Chapter 1 of the test story.

TAGS (5):
furry, anthro, test, story, fiction

INKBUNNY TAGS (Categorized):

Sex/Gender:
male, female, mf, heterosexual

Species:
anthro, furry, wolf, canine

Themes/Kinks:
test, fiction, romance, drama

Other Keywords:
story, original, complete

WATTPAD TAGS (5 max):
furry anthro test story fiction

=============================================
PART 2 OF 2: "The End" (~40 words)
=============================================

DESCRIPTION:
The conclusion.

TAGS (3):
furry, anthro, ending
""",
        encoding="utf-8",
    )

    return tmp_path


@pytest.fixture
def upload_file(tmp_path):
    """Create a small test file for upload testing."""
    f = tmp_path / "test_upload.txt"
    f.write_text("This is a test story for uploading.", encoding="utf-8")
    return str(f)
