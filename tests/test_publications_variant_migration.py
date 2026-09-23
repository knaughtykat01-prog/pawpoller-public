"""`publications` learns which render a row is (backlog VARSPLIT, spec 004).

Until now a piece could hold one submission per (platform, account): the UNIQUE key had
no room for a second, so posting two renders of one piece to one site meant the second
`upsert_publication` UPDATED the first and the first post's URL, rating and history were
lost. `masterpiece_members` has always modelled it correctly — one row per upload,
carrying `variant_key`. This is publications catching up.

The migration runs on the busiest table in the app, and `posting_queue` and `posting_log`
both hold `pub_id` foreign keys into it. So the thing these tests actually guard is not
the new column — it is that **no row and no pub_id changed** on the way through.
"""
from __future__ import annotations

import sqlite3

import pytest

from database import db as db_module
from database import posting_queries


def _legacy_publications(conn):
    """The table as it stood BEFORE this migration (post-content_type)."""
    conn.execute(
        """CREATE TABLE publications (
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
        )""")


@pytest.fixture
def legacy(tmp_path):
    """A pre-migration DB with a row of every content_type, plus an FK referrer."""
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    _legacy_publications(conn)
    rows = [
        ("story", "Sample Story", 0, "fa", 1, "111", "https://example.test/1"),
        ("story", "Sample Story", 1, "fa", 1, "112", "https://example.test/2"),
        ("artwork", "Sample Piece", 0, "fa", 1, "222", "https://example.test/3"),
        ("artwork", "Sample Piece", 0, "e621", 2, "333", "https://example.test/4"),
        ("post", "p-9", 0, "bsky", 3, "444", "https://example.test/5"),
    ]
    for ct, name, ch, plat, acct, ext, url in rows:
        conn.execute(
            "INSERT INTO publications (content_type, story_name, chapter_index, platform,"
            " account_id, external_id, external_url, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'posted')",
            (ct, name, ch, plat, acct, ext, url))
    # A referrer, so a rebuild that renumbers pub_id is caught rather than assumed safe.
    conn.execute("CREATE TABLE posting_log (log_id INTEGER PRIMARY KEY, pub_id INTEGER)")
    conn.execute("INSERT INTO posting_log (log_id, pub_id) VALUES (1, 3)")
    conn.commit()
    return conn


def _snapshot(conn):
    return {r["pub_id"]: dict(r) for r in conn.execute(
        "SELECT pub_id, content_type, story_name, chapter_index, platform, account_id,"
        " external_id, external_url FROM publications")}


class TestTheMigration:
    def test_every_row_and_every_pub_id_survives(self, legacy):
        before = _snapshot(legacy)
        db_module._rebuild_publications_variant_key(legacy, None)
        after = _snapshot(legacy)
        assert after == before, "a rebuild must not lose, reorder or renumber rows"

    def test_the_foreign_key_still_resolves(self, legacy):
        db_module._rebuild_publications_variant_key(legacy, None)
        row = legacy.execute(
            "SELECT p.external_id FROM posting_log l JOIN publications p ON p.pub_id = l.pub_id"
        ).fetchone()
        assert row is not None and row["external_id"] == "222"

    def test_existing_rows_get_the_empty_render(self, legacy):
        db_module._rebuild_publications_variant_key(legacy, None)
        keys = {r[0] for r in legacy.execute("SELECT variant_key FROM publications")}
        assert keys == {""}, "a row posted before renders existed is the primary"

    def test_the_new_unique_key_is_in_the_stored_ddl(self, legacy):
        db_module._rebuild_publications_variant_key(legacy, None)
        ddl = legacy.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='publications'"
        ).fetchone()[0]
        assert "variant_key" in ddl
        assert "account_id, variant_key)" in ddl.replace("\n", " ")

    def test_it_is_idempotent(self, legacy):
        db_module._rebuild_publications_variant_key(legacy, None)
        before = _snapshot(legacy)
        db_module._rebuild_publications_variant_key(legacy, None)
        assert _snapshot(legacy) == before

    def test_it_no_ops_without_the_table(self, tmp_path):
        conn = sqlite3.connect(tmp_path / "empty.db")
        db_module._rebuild_publications_variant_key(conn, None)   # must not raise


class TestTwoRendersCoexist:
    """The whole point: one piece, one platform, one account, two submissions."""

    @pytest.fixture
    def conn(self, legacy):
        db_module._rebuild_publications_variant_key(legacy, None)
        return legacy

    def test_two_renders_are_two_rows(self, conn):
        a = posting_queries.upsert_publication(
            conn, "Sample Piece", 0, "fa", account_id=1, content_type="artwork",
            external_id="900", variant_key="")
        b = posting_queries.upsert_publication(
            conn, "Sample Piece", 0, "fa", account_id=1, content_type="artwork",
            external_id="901", variant_key="alt")
        assert a != b
        ids = {r[0] for r in conn.execute(
            "SELECT external_id FROM publications WHERE story_name = 'Sample Piece'"
            " AND platform = 'fa'")}
        assert ids == {"900", "901"}

    def test_the_same_render_twice_still_updates(self, conn):
        """Re-posting one render must not accumulate rows — that was never the bug."""
        a = posting_queries.upsert_publication(
            conn, "Sample Piece", 0, "fa", account_id=1, content_type="artwork",
            external_id="900", variant_key="alt")
        b = posting_queries.upsert_publication(
            conn, "Sample Piece", 0, "fa", account_id=1, content_type="artwork",
            external_id="900", variant_key="alt", status="posted")
        assert a == b
        n = conn.execute(
            "SELECT COUNT(*) FROM publications WHERE story_name = 'Sample Piece'"
            " AND platform = 'fa' AND variant_key = 'alt'").fetchone()[0]
        assert n == 1

    def test_a_render_does_not_collide_with_the_primary(self, conn):
        """The pre-migration failure, pinned: the second write used to overwrite."""
        posting_queries.upsert_publication(
            conn, "Sample Piece", 0, "fa", account_id=1, content_type="artwork",
            external_id="primary-id", variant_key="")
        posting_queries.upsert_publication(
            conn, "Sample Piece", 0, "fa", account_id=1, content_type="artwork",
            external_id="alt-id", variant_key="alt")
        primary = conn.execute(
            "SELECT external_id FROM publications WHERE story_name = 'Sample Piece'"
            " AND platform = 'fa' AND variant_key = ''").fetchone()[0]
        assert primary == "primary-id", "the alt must not have overwritten the primary"

    def test_stories_are_untouched(self, conn):
        """Stories have no renders and chapter_index is load-bearing — FR-005."""
        posting_queries.upsert_publication(
            conn, "Sample Story", 0, "fa", account_id=1, content_type="story",
            external_id="new-111")
        n = conn.execute(
            "SELECT COUNT(*) FROM publications WHERE story_name = 'Sample Story'"
            " AND chapter_index = 0").fetchone()[0]
        assert n == 1, "a story upsert with no render must still update in place"


class TestTheEditPathKeepsBothRows:
    """The High from the 4.34.0 review, pinned BEHAVIOURALLY.

    `update_artwork` read the recorded render correctly, refused a render the piece no
    longer declared, built the right package and edited the right submission — and then
    wrote the result back with no `variant_key`, so the default `''` matched the
    PRIMARY's row and overwrote its external_id, url, title, tags and rating with the
    alternate's. Press "Sync all" on a piece posted as two renders and the primary's
    submission is lost from the registry while staying live on the platform.

    That is verbatim the harm the migration exists to stop, reached from the edit side.

    ⚠ The behavioural guard lives in tests/test_masterpiece_sync.py
    (`test_sync_all_does_not_overwrite_the_primarys_publication`), because it needs the
    archive + poster-stub harness there. These tests state the property; that one proves
    the edit path honours it.

    The source-inspection tests in test_render_split.py could not see this: they scope to
    `post_artwork`, and the one that checks the edit path asserts only that the string
    `m.get("variant_key")` appears somewhere in `update_artwork` — which it does, for the
    package build, twenty lines above the write that ignored it.
    """

    @pytest.fixture
    def posted(self, legacy):
        db_module._rebuild_publications_variant_key(legacy, None)
        for vk, ext in (("", "111"), ("alt", "222")):
            posting_queries.upsert_publication(
                legacy, "Two Renders", 0, "fa", account_id=1, content_type="artwork",
                external_id=ext, external_url=f"https://example.test/{ext}",
                title_used="Piece" if not vk else "Piece (Alt)", variant_key=vk)
        return legacy

    def _rows(self, conn):
        return {r["variant_key"]: r["external_id"] for r in conn.execute(
            "SELECT variant_key, external_id FROM publications "
            "WHERE story_name = 'Two Renders'")}

    def test_the_fixture_really_has_two_rows(self, posted):
        assert self._rows(posted) == {"": "111", "alt": "222"}

    def test_a_write_naming_the_alt_leaves_the_primary_alone(self, posted):
        """NOTE: this tests `upsert_publication`, NOT the edit path.

        It is kept because it states the property the edit path depends on, but it must
        not be mistaken for coverage of `update_artwork` — it never calls it. The real
        guard is `test_sync_all_does_not_overwrite_the_primarys_publication` in
        tests/test_masterpiece_sync.py, which drives the actual function; that one fails
        against a hardcoded `variant_key=""` and this one does not.
        """
        posting_queries.upsert_publication(
            posted, "Two Renders", 0, "fa", account_id=1, content_type="artwork",
            external_id="222", external_url="https://example.test/222",
            title_used="Piece (Alt) edited", variant_key="alt")
        assert self._rows(posted) == {"": "111", "alt": "222"}, \
            "the primary's row must not be touched by an edit to the alt"

    def test_an_edit_that_forgets_the_render_would_destroy_the_primary(self, posted):
        """The bug itself, so the fix cannot silently regress.

        Writing the alt's result with no variant_key matches the primary's row — this
        asserts that IS destructive, which is why `update_artwork` must pass it.
        """
        posting_queries.upsert_publication(
            posted, "Two Renders", 0, "fa", account_id=1, content_type="artwork",
            external_id="222", external_url="https://example.test/222",
            title_used="Piece (Alt)")          # ← no variant_key, the defect
        assert self._rows(posted)[""] == "222", (
            "if this ever stops being destructive the guard below is no longer needed")

    def test_the_edit_path_passes_the_render_it_read(self):
        """A cheap source guard, and deliberately NOT the real one.

        A hardcoded `variant_key=""` satisfies this while fully reintroducing the bug —
        which is exactly how the first round of tests missed it. It survives only as a
        fast signal; the behavioural test in tests/test_masterpiece_sync.py is the guard.
        """
        import inspect
        from posting import manager
        src = inspect.getsource(manager.update_artwork)
        i = src.index("upsert_publication(")
        assert "variant_key=" in src[i:i + 1200], \
            "update_artwork's publication write must name the render it edited"
