"""Own-account comment detection (2.192.0).

Before this, a comment written by the posting account counted as NEW everywhere:
toasts, Telegram, new_comments_found, the Inbox "to answer" badge, and the Top
Fans leaderboard (where you ranked as your own top fan). IB and FA had no filter
at all; the four A1 platforms had one that compared only the local part of a
handle, so a Mastodon stranger on another instance matched us.

The cross-instance false positive is the headline regression test here — it is
the defect that made the old filter actively wrong rather than merely absent.
"""
import config
from database.db import get_connection
from database import analytics_queries, fa_queries, inbox_queries, queries
from polling import self_comment


# ── normalisation ─────────────────────────────────────────────

def test_normalise_handle_trims_cases_and_one_leading_at():
    assert self_comment.normalise_handle("  @Tester  ") == "tester"
    assert self_comment.normalise_handle("SAM") == "sam"
    assert self_comment.normalise_handle("") == ""
    assert self_comment.normalise_handle(None) == ""
    # An interior '@' is data, not a separator — never split on it.
    assert self_comment.normalise_handle("@sam@ours.social") == "sam@ours.social"


def test_empty_handle_set_never_matches():
    """Unknown identity must read as 'a stranger', never as 'everyone is me'."""
    assert self_comment.is_own_author("anyone", set()) is False
    assert self_comment.is_own_author("", set()) is False


# ── the regression that motivated the rewrite ─────────────────

def test_mastodon_other_instance_is_not_us():
    conn = get_connection()
    try:
        config.save_settings({"mast_own_handle": "sam@ours.social"})
        handles = self_comment.own_handles(conn, "mast")

        # Same local part, DIFFERENT instance = a stranger. The pre-2.192 code
        # compared author.split("@")[0] on both sides and called this us.
        assert self_comment.is_own_author("sam@some.other.instance", handles) is False
        assert self_comment.is_own_author("@sam@other.social", handles) is False

        # Us, fully qualified.
        assert self_comment.is_own_author("sam@ours.social", handles) is True
        # Us, as Mastodon reports home-instance accounts: bare local part. This
        # is why OUR side is widened instead of the incoming author narrowed.
        assert self_comment.is_own_author("sam", handles) is True
    finally:
        conn.close()


def test_bsky_email_login_is_not_treated_as_a_handle():
    """bsky_identifier is a LOGIN field and may hold an email address."""
    conn = get_connection()
    try:
        config.save_settings({"bsky_identifier": "owner@example.com"})
        handles = self_comment.own_handles(conn, "bsky")
        assert "owner@example.com" not in handles
        # An email must not make every commenter named "ownerhandle" us.
        assert self_comment.is_own_author("ownerhandle", handles) is False

        # A real handle (no '@') is trusted.
        config.save_settings({"bsky_identifier": "sam.bsky.social"})
        handles = self_comment.own_handles(conn, "bsky")
        assert self_comment.is_own_author("sam.bsky.social", handles) is True
    finally:
        conn.close()


def test_own_handles_reads_the_platform_identity_keys():
    conn = get_connection()
    try:
        config.save_settings({"username": "secondfur", "fa_username": "SecondFur",
                              "e621_username": "kithe", "da_target_user": "KitheArt"})
        assert self_comment.is_own_author(
            "secondfur", self_comment.own_handles(conn, "ib")) is True
        assert self_comment.is_own_author(
            "secondfur", self_comment.own_handles(conn, "fa")) is True
        assert self_comment.is_own_author(
            "kithe", self_comment.own_handles(conn, "e621")) is True
        assert self_comment.is_own_author(
            "kitheart", self_comment.own_handles(conn, "da")) is True
        # Platforms don't leak identities into each other.
        assert self_comment.is_own_author(
            "kithe", self_comment.own_handles(conn, "ib")) is False
    finally:
        conn.close()


# ── ingestion: IB ─────────────────────────────────────────────

def _seed_ib(conn, comment_id, username):
    return queries.upsert_comment(conn, {
        "comment_id": comment_id, "submission_id": 555, "username": username,
        "comment_text": "hi", "commented_at": "2026-07-29",
    }, is_own=self_comment.is_own_author(
        username, self_comment.own_handles(conn, "ib")))


def test_ib_own_comment_stored_flagged_and_stranger_untouched():
    conn = get_connection()
    try:
        config.save_settings({"username": "secondfur"})
        conn.execute("INSERT INTO submissions (submission_id, title)"
                     " VALUES (555, 'My IB Piece')")
        conn.commit()

        assert _seed_ib(conn, 1001, "secondfur") is True   # ours — still STORED
        assert _seed_ib(conn, 1002, "ibfan") is True
        conn.commit()

        rows = {r["comment_id"]: r["is_own"]
                for r in conn.execute("SELECT comment_id, is_own FROM comments")}
        assert rows[1001] == 1
        assert rows[1002] == 0
    finally:
        conn.close()


def test_ib_recent_comments_excludes_ours():
    conn = get_connection()
    try:
        config.save_settings({"username": "secondfur"})
        conn.execute("INSERT INTO submissions (submission_id, title)"
                     " VALUES (555, 'My IB Piece')")
        conn.commit()
        _seed_ib(conn, 1001, "secondfur")
        _seed_ib(conn, 1002, "ibfan")
        conn.commit()

        names = {c["username"] for c in queries.get_recent_comments(conn)}
        assert names == {"ibfan"}
    finally:
        conn.close()


# ── ingestion: FA ─────────────────────────────────────────────

def test_fa_batch_flags_ours_only():
    conn = get_connection()
    try:
        config.save_settings({"fa_username": "SecondFur"})
        conn.execute("INSERT INTO fa_submissions (submission_id, title)"
                     " VALUES (777, 'My FA Piece')")
        conn.commit()
        handles = self_comment.own_handles(conn, "fa")

        fa_queries.upsert_fa_comments_batch(conn, 0, [
            {"comment_id": "c1", "submission_id": 777, "username": "secondfur",
             "comment_text": "bump", "commented_at": "2026-07-29"},
            {"comment_id": "c2", "submission_id": 777, "username": "fafan",
             "comment_text": "nice", "commented_at": "2026-07-29"},
        ], handles)
        conn.commit()

        rows = {r["comment_id"]: r["is_own"]
                for r in conn.execute("SELECT comment_id, is_own FROM fa_comments")}
        assert rows["c1"] == 1        # case-insensitive match on OUR handle
        assert rows["c2"] == 0
        assert {c["username"] for c in fa_queries.get_fa_recent_comments(conn)} == {"fafan"}
    finally:
        conn.close()


# ── read-side: Top Fans ───────────────────────────────────────

def test_top_fans_excludes_our_own_account():
    """The most visible symptom: ranking as your own top fan."""
    conn = get_connection()
    try:
        config.save_settings({"username": "secondfur", "fa_username": "secondfur"})
        conn.execute("INSERT INTO submissions (submission_id, title)"
                     " VALUES (555, 'p')")
        conn.execute("INSERT INTO fa_submissions (submission_id, title)"
                     " VALUES (777, 'p')")
        conn.commit()
        # Three self-comments would otherwise outrank the single real fan.
        for cid in (1001, 1002, 1003):
            _seed_ib(conn, cid, "secondfur")
        _seed_ib(conn, 1004, "ibfan")
        fa_queries.upsert_fa_comments_batch(conn, 0, [
            {"comment_id": "c1", "submission_id": 777, "username": "secondfur",
             "comment_text": "x", "commented_at": "2026-07-29"},
        ], self_comment.own_handles(conn, "fa"))
        conn.commit()

        fans = {f["username"] for f in analytics_queries.get_top_fans(conn)}
        assert "secondfur" not in fans
        assert "ibfan" in fans
    finally:
        conn.close()


# ── read-side: the Inbox ──────────────────────────────────────

def _seed_platform(conn, cid, author, is_own):
    return inbox_queries.upsert_platform_comment(
        conn, "bsky", cid, "at://me/p1", author=author, body="b",
        commented_at="2026-07-29", permalink="https://bsky.app/x",
        submission_title="My Post", is_own=is_own)


def test_own_platform_comment_is_context_not_a_task():
    conn = get_connection()
    try:
        _seed_platform(conn, "r1", "sam.bsky.social", True)
        _seed_platform(conn, "r2", "fan1", False)

        items = {i["comment_id"]: i for i in inbox_queries.get_inbox(conn)}
        # Ours stays visible as thread context...
        assert items["r1"]["is_own"] is True
        # ...but is never something to answer, with no inbox_state row written.
        assert items["r1"]["handled"] is True
        assert items["r2"]["handled"] is False

        unhandled = inbox_queries.get_inbox(conn, unhandled_only=True)
        assert [i["comment_id"] for i in unhandled] == ["r2"]
    finally:
        conn.close()


def test_unhandled_badge_ignores_own_comments():
    conn = get_connection()
    try:
        for i in range(3):
            _seed_platform(conn, f"mine{i}", "sam.bsky.social", True)
        _seed_platform(conn, "theirs", "fan1", False)
        count = sum(1 for i in inbox_queries.get_inbox(conn) if not i["handled"])
        assert count == 1
    finally:
        conn.close()


# ── backfill ──────────────────────────────────────────────────

def test_backfill_flags_historical_rows_and_is_idempotent():
    """Rows captured before 2.192.0 (or before a handle was known) get fixed.

    This also covers the old filter's new-rows-only defect: it only ran inside
    `if is_new:`, so an already-stored self-comment could never be corrected.
    """
    conn = get_connection()
    try:
        conn.execute("INSERT INTO submissions (submission_id, title)"
                     " VALUES (555, 'p')")
        conn.commit()
        # Stored with NO identity configured → nothing flagged.
        _seed_ib(conn, 1001, "secondfur")
        _seed_ib(conn, 1002, "ibfan")
        _seed_platform(conn, "r1", "sam.bsky.social", False)
        conn.commit()
        assert conn.execute(
            "SELECT COUNT(*) FROM comments WHERE is_own = 1").fetchone()[0] == 0

        # Identity arrives (e.g. the account is connected), then backfill.
        config.save_settings({"username": "secondfur",
                              "bsky_own_handle": "sam.bsky.social"})
        flagged = self_comment.backfill_own_comments(conn)
        assert flagged.get("ib") == 1
        assert flagged.get("bsky") == 1

        assert conn.execute("SELECT is_own FROM comments WHERE comment_id = 1001"
                            ).fetchone()[0] == 1
        assert conn.execute("SELECT is_own FROM comments WHERE comment_id = 1002"
                            ).fetchone()[0] == 0
        assert inbox_queries.get_inbox(conn, unhandled_only=True) == [] or all(
            i["comment_id"] != "r1"
            for i in inbox_queries.get_inbox(conn, unhandled_only=True))

        # Re-running changes nothing.
        assert self_comment.backfill_own_comments(conn) == {}
    finally:
        conn.close()


def test_backfill_is_a_noop_without_a_known_handle():
    conn = get_connection()
    try:
        conn.execute("INSERT INTO submissions (submission_id, title)"
                     " VALUES (555, 'p')")
        conn.commit()
        _seed_ib(conn, 1001, "someone")
        conn.commit()
        assert self_comment.backfill_own_comments(conn) == {}
    finally:
        conn.close()
