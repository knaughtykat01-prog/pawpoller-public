"""Four small backlog items closed after 4.34.0 shipped.

TWSENS, VARMEMKEY and the two lows from the 4.34.0 security review
(PUBRENDERLOWS). Each is a few lines; each closes a way for something true of the
data to stop being true.
"""
from __future__ import annotations

import inspect
import sqlite3

import pytest

from database import masterpiece_queries as mq
from database import posting_queries


class TestAdultMicroblogPostsAreFlaggedOnX:
    """TWSENS. `create_tweet` gained a `sensitive` parameter in 4.3.7 and the artwork
    poster set it from the rating; the microblog path kept passing the default, so every
    post from the Posts hub went out unflagged whatever its rating said.

    The backlog said this was blocked because "a microblog post carries no rating". That
    was wrong — `posts.rating` has existed all along, and `post_publisher` already reads
    it for Bluesky's label, Mastodon's sensitive flag and Telegram's spoiler. X was the
    only one of the four that ignored it.
    """

    def test_the_x_branch_passes_the_rating_through(self):
        from posting import post_publisher
        src = inspect.getsource(post_publisher)
        i = src.index("r = await client.create_tweet(")
        assert "sensitive=(rating in _SENSITIVE_RATINGS)" in src[i:i + 300]

    def test_every_platform_on_the_path_now_reads_the_rating(self):
        """The asymmetry was the bug — pin all four so it cannot come back alone."""
        from posting import post_publisher
        src = inspect.getsource(post_publisher)
        assert "labels=_BSKY_LABELS.get(rating)" in src          # Bluesky
        assert src.count("sensitive=(rating in _SENSITIVE_RATINGS)") == 2  # Mastodon + X
        assert "spoiler=(rating in _SENSITIVE_RATINGS)" in src    # Telegram

    def test_mature_and_adult_both_count(self):
        from posting.post_publisher import _SENSITIVE_RATINGS
        assert "adult" in _SENSITIVE_RATINGS and "mature" in _SENSITIVE_RATINGS
        assert "general" not in _SENSITIVE_RATINGS


class TestDemotingAVariantDoesNotLieAboutItsPosts:
    """VARMEMKEY. Demoting a variant kept the FILE but re-keyed its members to primary
    (''), which asserts those live submissions hold the piece's own image. They hold the
    demoted render's. An edit then pushed the primary's rating and tags over them — and
    an edit sets skip_content_refresh, so the image stays. On a piece rated below one of
    its renders that is a rating downgrade on live adult work.
    """

    def test_the_demote_route_no_longer_rekeys(self):
        src = open("routes/masterpieces_api.py", encoding="utf-8").read()
        i = src.index("def delete_variant")
        block = src[i:i + 2200]
        assert "clear_variant_members" not in block, \
            "demoting must not claim those submissions hold the primary"
        assert "no longer declares" in block or "VARMEMKEY" in block

    def test_the_helper_warns_what_it_asserts(self):
        doc = mq.clear_variant_members.__doc__ or ""
        assert "positive claim" in doc

    def test_the_edit_path_refuses_a_render_the_piece_no_longer_declares(self):
        """Which is what makes leaving the key safe rather than merely honest."""
        from posting import manager
        src = inspect.getsource(manager.update_artwork)
        assert "no longer declares" in src


class TestThePublicationLookupIsDeterministic:
    """PUBRENDERLOWS (1). The key stopped being unique when a piece could hold several
    renders on one site, so an unqualified lookup returned whichever row SQLite yielded
    — making an audit-trail mislink depend on insert order."""

    @pytest.fixture
    def conn(self, tmp_path):
        c = sqlite3.connect(tmp_path / "t.db")
        c.row_factory = sqlite3.Row
        c.execute("""CREATE TABLE publications (
            pub_id INTEGER PRIMARY KEY AUTOINCREMENT, content_type TEXT, story_name TEXT,
            chapter_index INTEGER, platform TEXT, account_id INTEGER,
            variant_key TEXT NOT NULL DEFAULT '', external_id TEXT, status TEXT)""")
        # the alt inserted FIRST, so "whichever comes back" is the wrong one
        for vk, ext in (("alt", "222"), ("", "111"), ("clean", "333")):
            c.execute("INSERT INTO publications (content_type, story_name, chapter_index,"
                      " platform, account_id, variant_key, external_id, status) "
                      "VALUES ('artwork', 'P', 0, 'fa', 1, ?, ?, 'posted')", (vk, ext))
        c.commit()
        return c

    def test_the_primary_wins_a_tie(self, conn):
        row = posting_queries.get_publication_by_story(
            conn, "P", 0, "fa", account_id=1, content_type="artwork")
        assert row["external_id"] == "111", "insert order must not decide this"

    def test_a_named_render_is_returned_exactly(self, conn):
        row = posting_queries.get_publication_by_story(
            conn, "P", 0, "fa", account_id=1, content_type="artwork", variant_key="clean")
        assert row["external_id"] == "333"

    def test_an_unknown_render_is_not_silently_substituted(self, conn):
        assert posting_queries.get_publication_by_story(
            conn, "P", 0, "fa", account_id=1, content_type="artwork",
            variant_key="nope") is None


class TestTheRebuildSwapIsOneTransaction:
    """PUBRENDERLOWS (2). `_run_table_rebuilds` runs in autocommit (needed for the PRAGMA
    foreign_keys toggle), so `DROP TABLE publications` committed on its own. A crash in
    that window orphaned the new table, and the next boot's CREATE TABLE IF NOT EXISTS
    made an empty `publications` — no error, no history.

    Fixed for all three rebuilds at once: fixing one would have been worse than fixing
    none, because it makes the other two look deliberate.
    """

    def test_all_three_swaps_are_wrapped(self):
        from database import db as db_module
        src = inspect.getsource(db_module)
        for fn in ("_rebuild_publications", "_rebuild_publications_content_type",
                   "_rebuild_publications_variant_key"):
            body = inspect.getsource(getattr(db_module, fn))
            assert 'conn.execute("BEGIN")' in body, f"{fn} swap is not atomic"
            assert "ROLLBACK" in body, f"{fn} does not roll back"
            assert body.index('conn.execute("BEGIN")') < body.index("DROP TABLE publications")

    def test_it_does_not_open_a_second_transaction(self):
        """sqlite refuses a nested BEGIN outright, so a caller that already holds one
        (a test, a nested migration) must be detected rather than assumed."""
        from database import db as db_module
        for fn in ("_rebuild_publications", "_rebuild_publications_content_type",
                   "_rebuild_publications_variant_key"):
            body = inspect.getsource(getattr(db_module, fn))
            assert "conn.in_transaction" in body, f"{fn} would fail inside a transaction"

    def test_a_failed_swap_leaves_the_original_table(self, tmp_path):
        """The behaviour the wrapper buys: no half-state."""
        from database import db as db_module
        c = sqlite3.connect(tmp_path / "x.db")
        c.isolation_level = None                       # autocommit, as production runs
        c.execute("CREATE TABLE publications (pub_id INTEGER PRIMARY KEY, story_name TEXT)")
        c.execute("INSERT INTO publications (story_name) VALUES ('keep me')")
        # No content_type column and no *_new table: the vk rebuild will raise part-way.
        with pytest.raises(Exception):
            db_module._rebuild_publications_variant_key(c, None)
            c.execute("DROP TABLE publications_vk_new")
            raise RuntimeError("forced")
        rows = [r[0] for r in c.execute("SELECT story_name FROM publications")]
        assert rows == ["keep me"], "the original table must survive a failed swap"
