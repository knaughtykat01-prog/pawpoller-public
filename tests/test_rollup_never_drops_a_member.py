"""A linked member must never be invisible in "Published to" (3.9.11).

Reported as "I've posted to multiple sites and it hasn't updated it". The
members were in the database — DeviantArt included — and the page showed three
rows out of five.

``rollup_members`` resolved each member through ``_location_from_submission``,
which returns ``None`` when the platform's own submission table has no row for
that id and no URL was passed. The rollup then dropped it. So a member only
appeared once the POLLER had stored it, and one whose id could never match was
invisible forever:

  * ``masterpiece_members`` for DA held the publish call's **UUID**
    (``DB44F5EB-…``), because that is what DA returns as ``deviationid``;
  * ``da_submissions`` keys on the **numeric** deviation id (``1370480056``),
    which is what the poller stores and what the public URL contains.

They never join. The piece was live on DeviantArt and PawPoller said it wasn't.

The publication row already holds a working link and the posted title, so it is
the fallback; and a member with neither still renders, because "linked but not
yet measured" is a true statement and "not published" is not.
"""
from __future__ import annotations

from database.db import get_connection
from database import masterpiece_queries as mq


def _publish(conn, name, platform, external_id, url, title, status="posted"):
    conn.execute(
        "INSERT INTO publications (content_type, story_name, chapter_index, platform, "
        "external_id, external_url, title_used, status) "
        "VALUES ('artwork', ?, 0, ?, ?, ?, ?, ?)",
        (name, platform, external_id, url, title, status))


def test_the_deviantart_shape_that_caused_this():
    """Member keyed by UUID, poller keyed by number — the row must still show."""
    conn = get_connection()
    name = "RollupDA"
    mq.add_member(conn, name, "da", "DB44F5EB-4612-DC4F-D9A5-0540011903B9",
                  account_id=7, role="crosspost", linked_via="publication")
    _publish(conn, name, "da", "DB44F5EB-4612-DC4F-D9A5-0540011903B9",
             "https://www.deviantart.com/secondfur/art/Blows-a-Kiss-1370480056",
             "Blows a Kiss ~")
    conn.commit()

    locs = mq.rollup_members(conn, name)["locations"]
    assert len(locs) == 1
    da = locs[0]
    assert da["platform"] == "da"
    assert da["url"].endswith("Blows-a-Kiss-1370480056"), "the link must work"
    assert da["title"] == "Blows a Kiss ~"
    # Counts are genuinely unknown until the poller runs — blank, not zero.
    assert da["stats"]["views"] is None
    assert da["stats"]["favorites"] is None
    conn.close()


def test_every_member_produces_exactly_one_location():
    """The invariant the bug broke: members in, same number of rows out."""
    conn = get_connection()
    name = "RollupCount"
    mq.add_member(conn, name, "da", "UUID-A", account_id=7, linked_via="publication")
    mq.add_member(conn, name, "fn", "1896469", account_id=28, linked_via="publication")
    mq.add_member(conn, name, "fa", "999999", account_id=15, linked_via="phash")
    _publish(conn, name, "da", "UUID-A", "https://da/art/x-1", "Titled")
    conn.commit()

    members = mq.get_members(conn, name)
    locs = mq.rollup_members(conn, name)["locations"]
    assert len(locs) == len(members) == 3
    assert {l["platform"] for l in locs} == {"da", "fn", "fa"}
    conn.close()


def test_a_member_with_no_publication_and_no_poll_still_appears():
    """Nothing to link to is not the same as not published."""
    conn = get_connection()
    name = "RollupBare"
    mq.add_member(conn, name, "ib", "555", account_id=3, linked_via="manual")
    conn.commit()

    locs = mq.rollup_members(conn, name)["locations"]
    assert len(locs) == 1
    assert locs[0]["platform"] == "ib"
    assert locs[0]["submission_id"] == "555"
    assert locs[0]["url"] == ""
    assert locs[0]["stats"]["views"] is None
    conn.close()


def test_a_failed_post_is_not_used_as_a_fallback():
    """A failed publication has no live page behind it; borrowing its URL would
    put a dead link in the list."""
    conn = get_connection()
    name = "RollupFailed"
    mq.add_member(conn, name, "da", "UUID-F", account_id=7, linked_via="publication")
    _publish(conn, name, "da", "UUID-F", "https://da/art/never", "Nope", status="failed")
    conn.commit()

    locs = mq.rollup_members(conn, name)["locations"]
    assert len(locs) == 1, "still visible..."
    assert locs[0]["url"] == "", "...but not linked to a post that never happened"
    conn.close()


def test_role_and_linked_via_survive_the_fallback_path():
    """These drive the CROSSPOST / POST-ONLY badges; the fallback branch must
    not quietly lose them."""
    conn = get_connection()
    name = "RollupBadges"
    mq.add_member(conn, name, "da", "UUID-B", account_id=7,
                  role="original", linked_via="publication")
    conn.commit()

    loc = mq.rollup_members(conn, name)["locations"][0]
    assert loc["role"] == "original"
    assert loc["linked_via"] == "publication"
    conn.close()


def test_a_polled_row_still_wins_over_the_fallback():
    """The fallback is a floor, not a replacement — real stats must not be
    clobbered by the publication's blanks."""
    conn = get_connection()
    name = "RollupPolled"
    info = list(conn.execute("PRAGMA table_info(fa_submissions)"))
    cols = [r[1] for r in info]
    assert "submission_id" in cols
    # NOT NULL columns need a value; the rest can stay NULL.
    have = {r[1]: ("" if str(r[2]).upper().startswith(("TEXT", "VARCHAR")) else 0)
            if r[3] else None for r in info}
    have["submission_id"] = "4242"
    have["title"] = "Polled Title"
    if "account_id" in have:
        have["account_id"] = 15
    if "views" in have:
        have["views"] = 554
    conn.execute(
        f"INSERT INTO fa_submissions ({','.join(have)}) VALUES ({','.join('?' * len(have))})",
        list(have.values()))
    mq.add_member(conn, name, "fa", "4242", account_id=15, linked_via="publication")
    _publish(conn, name, "fa", "4242", "https://fallback/url", "Fallback Title")
    conn.commit()

    loc = mq.rollup_members(conn, name)["locations"][0]
    assert loc["title"] == "Polled Title", "the poller's row is the better source"
    if "views" in cols:
        assert loc["stats"]["views"] == 554
    conn.close()
