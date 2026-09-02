"""Folding a piece must not orphan its publications (3.16.0).

`junk` and `delete` are the loud ways to lose a piece. **Folding** was the quiet
one: `merge_masterpieces` moved `masterpiece_members` to the surviving work and
the route then `rmtree`'d the absorbed folder — but nothing touched
`publications`. Every fold therefore left rows whose `story_name` names a work
that no longer exists on disk.

Those rows are invisible **by construction**, which is why it went unnoticed for
so long:

  * every works list is built from `list_artworks()` / `list_stories()`, i.e.
    folders — so a publication naming a missing folder renders nowhere;
  * the *discovered* list excludes anything that HAS a publication row, so the
    hand-link picker cannot offer it either.

A prod audit found **41 of 86** artwork work-names in `publications` had no
folder. It bit hardest on IMPORTED art, because `artwork_importer` writes a
publication and **no member row** — so folding an imported piece moved nothing
at all. That is how FA 37056160 ("Embarrassed") ended up orphaned while the same
picture sat in the catalogue as *Growing Into It*, pooling none of its stats.
"""
from __future__ import annotations

import pytest

from database import masterpiece_queries as mq


def _pub(conn, story, platform, ext, account=0, ctype="artwork"):
    conn.execute(
        "INSERT INTO publications (content_type, story_name, chapter_index, platform, "
        "account_id, external_id, status) VALUES (?, ?, 0, ?, ?, ?, 'posted')",
        (ctype, story, platform, account, ext))
    conn.commit()


def _members(conn, name):
    return {(m["platform"], str(m["submission_id"])) for m in mq.get_members(conn, name)}


def _story_of(conn, platform, ext):
    r = conn.execute(
        "SELECT story_name FROM publications WHERE platform = ? AND external_id = ?",
        (platform, ext)).fetchone()
    return r["story_name"] if r else None


# ── the fix ──────────────────────────────────────────────────────

def test_a_folded_works_publication_follows_it(db_conn):
    _pub(db_conn, "Drop", "fa", "111")
    mq.absorb_publications(db_conn, "Keep", "Drop")
    assert _story_of(db_conn, "fa", "111") == "Keep"


def test_folding_creates_the_member_row_that_makes_stats_pool(db_conn):
    """The actual user-visible harm was not the stale name — it was that the
    views and faves stopped counting toward anything. `publications` does not
    pool stats; `masterpiece_members` does."""
    _pub(db_conn, "Drop", "fa", "111")
    res = mq.absorb_publications(db_conn, "Keep", "Drop")
    assert ("fa", "111") in _members(db_conn, "Keep")
    assert res["members_added"] == 1


def test_imported_art_is_the_worst_case_and_is_covered(db_conn):
    """`artwork_importer` writes a publication and NO member row, so the old
    fold moved literally nothing for imported pieces — the exact shape of the
    FA 37056160 / *Growing Into It* bug."""
    _pub(db_conn, "Embarrassed", "fa", "37056160")
    assert _members(db_conn, "Growing_Into_It") == set()
    mq.absorb_publications(db_conn, "Growing_Into_It", "Embarrassed")
    assert ("fa", "37056160") in _members(db_conn, "Growing_Into_It")
    assert _story_of(db_conn, "fa", "37056160") == "Growing_Into_It"


def test_an_existing_member_is_not_duplicated(db_conn):
    _pub(db_conn, "Drop", "fa", "111")
    mq.add_member(db_conn, "Keep", "fa", "111")
    res = mq.absorb_publications(db_conn, "Keep", "Drop")
    assert res["members_added"] == 0
    assert len(_members(db_conn, "Keep")) == 1


def test_several_platforms_all_travel(db_conn):
    for plat, ext in (("fa", "1"), ("e621", "2"), ("ib", "3")):
        _pub(db_conn, "Drop", plat, ext)
    res = mq.absorb_publications(db_conn, "Keep", "Drop")
    assert res["moved"] == 3
    assert _members(db_conn, "Keep") == {("fa", "1"), ("e621", "2"), ("ib", "3")}


# ── the collision the UNIQUE key forces ──────────────────────────

def test_a_colliding_publication_is_kept_not_destroyed(db_conn):
    """UNIQUE(content_type, story_name, chapter_index, platform, account_id)
    allows only ONE publication per work+platform+account, so two works posted
    to the same FA account cannot both re-point. Losing the row would throw away
    `first_posted_at` and the posted title, so it stays put — and the member
    created alongside it means the stats still pool."""
    _pub(db_conn, "Keep", "fa", "111")
    _pub(db_conn, "Drop", "fa", "222")
    res = mq.absorb_publications(db_conn, "Keep", "Drop")
    assert res["blocked"] == 1
    assert res["moved"] == 0
    assert _story_of(db_conn, "fa", "222") == "Drop"      # not deleted
    assert ("fa", "222") in _members(db_conn, "Keep")     # but it DOES pool


def test_a_collision_does_not_stop_the_others_moving(db_conn):
    _pub(db_conn, "Keep", "fa", "111")
    _pub(db_conn, "Drop", "fa", "222")
    _pub(db_conn, "Drop", "e621", "333")
    res = mq.absorb_publications(db_conn, "Keep", "Drop")
    assert res["blocked"] == 1 and res["moved"] == 1
    assert _story_of(db_conn, "e621", "333") == "Keep"


def test_publications_are_never_deleted(db_conn):
    before = db_conn.execute("SELECT COUNT(*) FROM publications").fetchone()[0]
    _pub(db_conn, "Keep", "fa", "111")
    _pub(db_conn, "Drop", "fa", "222")
    mq.absorb_publications(db_conn, "Keep", "Drop")
    after = db_conn.execute("SELECT COUNT(*) FROM publications").fetchone()[0]
    assert after == before + 2


def test_a_publication_with_no_external_id_gets_no_member(db_conn):
    """A draft row has no submission to point at; inventing a member with an
    empty submission_id would create an unjoinable row."""
    _pub(db_conn, "Drop", "fa", "")
    res = mq.absorb_publications(db_conn, "Keep", "Drop")
    assert res["members_added"] == 0
    assert _members(db_conn, "Keep") == set()


# ── surfacing what already exists ────────────────────────────────

def test_orphans_are_the_publications_with_no_folder(db_conn):
    _pub(db_conn, "Real", "fa", "1")
    _pub(db_conn, "Vanished", "fa", "2")
    got = mq.orphan_publications(db_conn, {"Real"})
    assert [o["story_name"] for o in got] == ["Vanished"]


def test_an_orphan_reports_whether_its_upload_still_pools(db_conn):
    """Distinguishes "only the paperwork is stale" from "this post counts for
    nothing" — the difference between a tidy-up and a real loss."""
    _pub(db_conn, "Vanished", "fa", "2")
    _pub(db_conn, "AlsoGone", "fa", "3")
    mq.add_member(db_conn, "Some_Piece", "fa", "2")
    got = {o["story_name"]: o for o in mq.orphan_publications(db_conn, set())}
    assert got["Vanished"]["linked_to"] == "Some_Piece"
    assert got["AlsoGone"]["linked_to"] == ""


def test_nothing_is_an_orphan_when_every_work_exists(db_conn):
    _pub(db_conn, "Real", "fa", "1")
    assert mq.orphan_publications(db_conn, {"Real"}) == []


# ── the route that shows them ────────────────────────────────────

def test_the_unfiled_route_is_registered_before_the_catch_all():
    """`/unfiled-posts` and `/{name}` both match GET /api/masterpieces/…, and
    FastAPI resolves in REGISTRATION order — so a literal route declared after
    the catch-all is swallowed and 404s as "Masterpiece not found".

    This is not hypothetical: the first version of the endpoint was appended to
    the end of the file and did exactly that. `/mislink-audit` sits up top for
    the same reason.
    """
    import routes.masterpieces_api as m
    paths = [r.path for r in m.masterpieces_router.routes]
    literal = paths.index("/api/masterpieces/unfiled-posts")
    catch_all = paths.index("/api/masterpieces/{name}")
    assert literal < catch_all, (
        "/unfiled-posts must be registered before /{name} or it will 404")


def test_every_literal_get_route_precedes_the_catch_all():
    """Generalised: any future literal GET added below `/{name}` breaks the same
    way, silently."""
    import routes.masterpieces_api as m
    gets = [r.path for r in m.masterpieces_router.routes
            if "GET" in getattr(r, "methods", set())]
    if "/api/masterpieces/{name}" not in gets:
        pytest.skip("catch-all route not present")
    catch_all = gets.index("/api/masterpieces/{name}")
    after = [p for p in gets[catch_all + 1:] if "{" not in p]
    assert after == [], f"literal GET routes shadowed by /{{name}}: {after}"
