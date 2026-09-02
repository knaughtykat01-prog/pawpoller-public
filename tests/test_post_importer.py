"""Import discovered microblog posts into the Posts module (2.157.0).

The discovered queue was mostly text tweets and its only import made an ARTWORK
(downloads an image, mints a folder) — meaningless with no image. These cover the
gate (what may become a post), idempotency, and account attribution.
"""
import pytest

from posting import post_importer as pi


# ── The gate: image → artwork, text → post, and only for microblogs ──

def _item(**kw):
    base = {"platform": "tw", "submission_id": "1", "kind": "text", "thumbnail_url": ""}
    base.update(kw)
    return base


def test_text_tweet_is_importable():
    assert pi.is_importable_post(_item()) is True


@pytest.mark.parametrize("platform", ["tw", "bsky", "mast", "thr", "tum"])
def test_every_microblog_platform_qualifies(platform):
    assert pi.is_importable_post(_item(platform=platform)) is True


@pytest.mark.parametrize("platform", ["sqw", "ao3", "da", "fa", "ib", "ws"])
def test_non_microblog_platforms_never_qualify(platform):
    # A SquidgeWorld text work or a thumbnail-less DeviantArt piece is a
    # story/artwork that happens to lack an image — NOT a post.
    assert pi.is_importable_post(_item(platform=platform)) is False


def test_image_bearing_item_is_not_a_post_import():
    # It has a home already: Import → artwork, ★ Master → Masterpiece.
    assert pi.is_importable_post(_item(thumbnail_url="http://t/1.jpg")) is False


def test_imageless_microblog_post_qualifies_even_when_kind_says_art():
    # Regression (2.157.1): classify_kind lists "post" among its ART hints, so
    # EVERY Bluesky post is tagged kind="art" whatever it contains. Gating on
    # kind hid the button from exactly the imageless posts it exists for — found
    # on live data, where a bsky and a mast item fell through to "neither".
    # No image ⇒ nothing for the artwork path to download ⇒ it's a text post.
    assert pi.is_importable_post(_item(platform="bsky", kind="art")) is True
    assert pi.is_importable_post(_item(platform="mast", kind="art")) is True


def test_image_bearing_microblog_post_still_goes_to_artwork():
    # The image, not `kind`, is what routes it.
    assert pi.is_importable_post(
        _item(platform="bsky", kind="art", thumbnail_url="http://t/1.jpg")) is False


def test_gate_handles_empty_input():
    assert pi.is_importable_post({}) is False
    assert pi.is_importable_post(None) is False


def test_import_rejects_non_microblog_platform():
    with pytest.raises(ValueError, match="not a microblog"):
        pi.import_post("sqw", "123")


# ── Import: body, account attribution, idempotency ──

@pytest.fixture
def tw_row(monkeypatch, tmp_path):
    """A stored tweet + an isolated DB, wired through the real query layer."""
    import config
    from database.db import init_db, get_connection
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    init_db()
    conn = get_connection()
    conn.execute(
        "INSERT INTO tw_submissions (submission_id, title, username, posted_at, "
        "description, link, rating, account_id) VALUES (?,?,?,?,?,?,?,?)",
        ("999", "Short title", "someuser", "2026-07-01 10:00:00",
         "The full tweet text.", "https://x.com/someuser/status/999", "general", 13),
    )
    conn.commit()
    conn.close()
    return "999"


def test_import_creates_post_from_the_stored_text(tw_row):
    from database.db import get_connection
    from database import posts_queries

    res = pi.import_post("tw", tw_row)
    assert res["status"] == "imported"

    conn = get_connection()
    try:
        post = posts_queries.get_post(conn, res["post_id"])
        # `description` is the full text; `title` is often truncated for display.
        assert post["body"] == "The full tweet text."
        pubs = posts_queries.get_post_publications(conn, res["post_id"])
        assert len(pubs) == 1
        assert pubs[0]["platform"] == "tw"
        assert pubs[0]["external_id"] == "999"
        assert pubs[0]["status"] == "posted"
        assert pubs[0]["external_url"] == "https://x.com/someuser/status/999"
        # Carries the SOURCE account, so the post lands on the right persona
        # instead of the platform default (the 2.96.0 "lumped personas" bug).
        assert pubs[0]["account_id"] == 13
    finally:
        conn.close()


def test_import_is_idempotent(tw_row):
    first = pi.import_post("tw", tw_row)
    second = pi.import_post("tw", tw_row)
    assert second["status"] == "skipped"
    assert second["post_id"] == first["post_id"]      # no duplicate minted


def test_imported_post_leaves_the_discovered_queue(tw_row):
    from database.db import get_connection
    from routes.submissions_api import get_discovered_unlinked

    conn = get_connection()
    try:
        before = get_discovered_unlinked(conn)
        assert ("tw", "999") in {(d["platform"], d["submission_id"]) for d in before}
    finally:
        conn.close()

    pi.import_post("tw", tw_row)

    # post_publications is a SEPARATE registry from publications — without it in
    # the exclusion set an imported tweet would sit in the queue forever.
    conn = get_connection()
    try:
        after = get_discovered_unlinked(conn)
        assert ("tw", "999") not in {(d["platform"], d["submission_id"]) for d in after}
    finally:
        conn.close()


def test_import_falls_back_to_title_when_description_is_empty(tw_row):
    from database.db import get_connection
    from database import posts_queries

    conn = get_connection()
    conn.execute("UPDATE tw_submissions SET description = '' WHERE submission_id = '999'")
    conn.commit()
    conn.close()

    res = pi.import_post("tw", "999")
    conn = get_connection()
    try:
        assert posts_queries.get_post(conn, res["post_id"])["body"] == "Short title"
    finally:
        conn.close()


def test_import_refuses_a_submission_with_no_text(tw_row):
    from database.db import get_connection
    conn = get_connection()
    conn.execute("UPDATE tw_submissions SET description='', title='' WHERE submission_id='999'")
    conn.commit()
    conn.close()

    with pytest.raises(ValueError, match="no text"):
        pi.import_post("tw", "999")


def test_import_unknown_submission_raises(tw_row):
    with pytest.raises(ValueError, match="No stored"):
        pi.import_post("tw", "does-not-exist")
