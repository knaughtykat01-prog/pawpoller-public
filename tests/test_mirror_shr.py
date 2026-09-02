"""Tests for the upward shared-table channel (mirroring Stage 3).

These run against **two separate databases**, because every bug worth catching
here is a bug about the boundary between them. A single-database test would
pass while the id offset that corrupted four production rows on 2026-08-12 went
straight through.

The weight is on:

* a row must land on the receiver's OWN ids, never the sender's,
* pushing twice must not duplicate anything,
* a delete must cross for the tables where it is the user's intent, and must
  NOT cross automatically for the one that touches artwork,
* a bundle claiming to carry a table this stage does not carry must be refused
  rather than partially applied.
"""
from __future__ import annotations

import sqlite3

import pytest

from mirror import registry, shr, tombstones


def _make_db(path):
    import config
    from database import db as db_mod
    saved = config.DB_PATH
    config.DB_PATH = path
    try:
        db_mod.init_db()
        conn = db_mod.get_connection()
    finally:
        config.DB_PATH = saved
    return conn


@pytest.fixture
def desktop(tmp_path):
    c = _make_db(tmp_path / "desktop.db")
    yield c
    c.close()


@pytest.fixture
def server(tmp_path):
    c = _make_db(tmp_path / "server.db")
    yield c
    c.close()


def _account(conn, platform, handle, *, default=False):
    cur = conn.execute(
        "INSERT INTO accounts (platform, label, handle, enabled, is_default, sort_order) "
        "VALUES (?, ?, ?, 1, ?, 0)",
        (platform, handle, handle, 1 if default else 0))
    conn.commit()
    return cur.lastrowid


def _push(desktop, server, **kw):
    """The whole channel, minus HTTP."""
    bundle = shr.export_bundle(desktop)
    return shr.apply_bundle(server, bundle, **kw)


# ── The boundary ──────────────────────────────────────────────

def test_account_lands_on_the_receivers_own_id(desktop, server):
    """The 2026-08-12 shape: the two installs allocate account_ids from their
    own sequences, so the same id means a different account on each box."""
    # Give the server three accounts so its next id is well past the desktop's.
    for p in ("ws", "sf", "sqw"):
        _account(server, p, f"{p}_user")
    desktop_id = _account(desktop, "fa", "secondfur")

    _push(desktop, server)

    row = server.execute(
        "SELECT account_id, platform FROM accounts WHERE lower(handle) = 'secondfur'"
    ).fetchone()
    assert row is not None
    assert row["platform"] == "fa"
    assert row["account_id"] != desktop_id, "the sender's id must not have crossed"


def test_publication_attaches_to_the_right_account_across_an_id_offset(desktop, server):
    """The failure this whole design exists to prevent: a publication landing on
    whichever account happens to hold the sender's id locally."""
    _account(server, "ws", "weasyl_user")     # server id 1..n
    _account(server, "sf", "sofurry_user")
    server_fa = _account(server, "fa", "secondfur")
    desktop_fa = _account(desktop, "fa", "secondfur")
    assert desktop_fa != server_fa, "fixture must actually create an offset"

    desktop.execute(
        "INSERT INTO publications (content_type, story_name, chapter_index, platform, "
        "account_id, external_id, status) VALUES ('story', 'Chosen', 1, 'fa', ?, '999', 'posted')",
        (desktop_fa,))
    desktop.commit()

    _push(desktop, server)

    row = server.execute(
        "SELECT account_id FROM publications WHERE story_name = 'Chosen'").fetchone()
    assert row["account_id"] == server_fa


def test_platform_is_never_overwritten_on_an_account(desktop, server):
    """An incoming row never writes its identity onto a local row of another
    platform — insert instead. §3.5.4's rule, re-asserted at this layer."""
    server_ws = _account(server, "ws", "shared_name")
    _account(desktop, "fa", "shared_name")

    _push(desktop, server)

    assert server.execute("SELECT platform FROM accounts WHERE account_id = ?",
                          (server_ws,)).fetchone()["platform"] == "ws"
    assert server.execute(
        "SELECT COUNT(*) FROM accounts WHERE lower(handle) = 'shared_name'"
    ).fetchone()[0] == 2


def test_incoming_default_does_not_displace_the_local_default(desktop, server):
    """The partial unique index allows one default per platform, and the local
    one is the account this install has actually been posting with."""
    server_default = _account(server, "fa", "server_fa", default=True)
    _account(desktop, "fa", "desktop_fa", default=True)

    _push(desktop, server)

    assert server.execute(
        "SELECT account_id FROM accounts WHERE platform='fa' AND is_default=1"
    ).fetchone()["account_id"] == server_default


# ── Idempotence ───────────────────────────────────────────────

def test_pushing_twice_changes_nothing(desktop, server):
    _account(desktop, "fa", "secondfur")
    desktop.execute("INSERT INTO personas (name, color) VALUES ('Kit', '#fff')")
    desktop.execute("INSERT INTO tags (name) VALUES ('feral')")
    desktop.execute("INSERT INTO masterpieces (name) VALUES ('Nesting Season')")
    desktop.execute(
        "INSERT INTO masterpiece_members (masterpiece_name, platform, submission_id) "
        "VALUES ('Nesting Season', 'fa', '12345')")
    desktop.execute(
        "INSERT INTO ignored_submissions (platform, submission_id) VALUES ('fa', '777')")
    desktop.commit()

    _push(desktop, server)
    counts_after_first = {
        t: server.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        for t in ("accounts", "personas", "tags", "masterpieces",
                  "masterpiece_members", "ignored_submissions")
    }
    _push(desktop, server)
    counts_after_second = {
        t: server.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        for t in counts_after_first
    }
    assert counts_after_first == counts_after_second


def test_submission_links_converge_on_their_member_set(desktop, server):
    """A link is nothing but an id and a timestamp, so its identity is what it
    links. Two installs that linked the same submissions made the same link."""
    for conn in (desktop, server):
        cur = conn.execute("INSERT INTO submission_links DEFAULT VALUES")
        lid = cur.lastrowid
        for platform, sid in (("fa", 111), ("ws", 222)):
            conn.execute(
                "INSERT INTO submission_link_members (link_id, platform, submission_id) "
                "VALUES (?, ?, ?)", (lid, platform, sid))
        conn.commit()

    _push(desktop, server)

    assert server.execute("SELECT COUNT(*) FROM submission_links").fetchone()[0] == 1


def test_a_genuinely_new_link_is_created(desktop, server):
    cur = desktop.execute("INSERT INTO submission_links DEFAULT VALUES")
    lid = cur.lastrowid
    desktop.execute("INSERT INTO submission_link_members (link_id, platform, submission_id) "
                    "VALUES (?, 'fa', 555)", (lid,))
    desktop.commit()

    _push(desktop, server)

    assert server.execute("SELECT COUNT(*) FROM submission_links").fetchone()[0] == 1
    assert server.execute(
        "SELECT submission_id FROM submission_link_members").fetchone()["submission_id"] == 555


# ── The hidden FK ─────────────────────────────────────────────

def test_collection_post_member_is_remapped_to_the_receivers_post_id(desktop, server):
    """collection_members.member_ref holds a stringified post_id for post
    members — §D2's hidden integer FK. Carried literally it would point at
    whatever post happens to hold that id on the server."""
    # Push the server's own id sequence forward so a literal copy would be wrong.
    for i in range(3):
        server.execute("INSERT INTO posts (body, created_at, updated_at) "
                       "VALUES (?, '2026-01-01 00:00:00', '')", (f"filler {i}",))
    server.commit()

    cur = desktop.execute(
        "INSERT INTO posts (body, rating, created_at, updated_at) "
        "VALUES ('new art up!', 'general', '2026-08-19 10:00:00', '2026-08-19 10:00:00')")
    post_id = cur.lastrowid
    cid = desktop.execute(
        "INSERT INTO collections (name) VALUES ('Launch day')").lastrowid
    desktop.execute(
        "INSERT INTO collection_members (collection_id, member_type, member_ref) "
        "VALUES (?, 'post', ?)", (cid, str(post_id)))
    desktop.commit()

    _push(desktop, server)

    row = server.execute(
        "SELECT cm.member_ref, p.body FROM collection_members cm "
        "JOIN posts p ON p.post_id = CAST(cm.member_ref AS INTEGER) "
        "WHERE cm.member_type = 'post'").fetchone()
    assert row is not None, "the post member did not resolve"
    assert row["body"] == "new art up!"
    assert row["member_ref"] != str(post_id), "the sender's post_id crossed literally"


def test_thread_parent_is_resolved_by_content_not_id(desktop, server):
    parent = desktop.execute(
        "INSERT INTO posts (body, created_at, updated_at) "
        "VALUES ('part one', '2026-08-19 10:00:00', '')").lastrowid
    desktop.execute(
        "INSERT INTO posts (body, created_at, updated_at, parent_post_id, thread_ordinal) "
        "VALUES ('part two', '2026-08-19 10:00:01', '', ?, 1)", (parent,))
    desktop.commit()

    _push(desktop, server)

    row = server.execute(
        "SELECT p.body AS child, q.body AS parent FROM posts p "
        "JOIN posts q ON q.post_id = p.parent_post_id WHERE p.thread_ordinal = 1").fetchone()
    assert row["child"] == "part two"
    assert row["parent"] == "part one"


# ── Insert-only tables ────────────────────────────────────────

def test_publications_never_overwrite_the_servers_row(desktop, server):
    """Upward, an update can only be a stale copy landing on fresher analytics."""
    _account(server, "fa", "secondfur", default=True)
    _account(desktop, "fa", "secondfur", default=True)
    for conn, status, ext in ((server, "posted", "SERVER-ID"), (desktop, "draft", "STALE")):
        conn.execute(
            "INSERT INTO publications (content_type, story_name, chapter_index, platform, "
            "account_id, external_id, status, word_count) "
            "VALUES ('story', 'Chosen', 1, 'fa', "
            "(SELECT account_id FROM accounts WHERE platform='fa'), ?, ?, 100)",
            (ext, status))
        conn.commit()

    _push(desktop, server)

    row = server.execute(
        "SELECT external_id, status FROM publications WHERE story_name='Chosen'").fetchone()
    assert row["external_id"] == "SERVER-ID"
    assert row["status"] == "posted"


def test_publications_still_insert_a_row_the_server_lacks(desktop, server):
    _account(server, "fa", "secondfur", default=True)
    _account(desktop, "fa", "secondfur", default=True)
    desktop.execute(
        "INSERT INTO publications (content_type, story_name, chapter_index, platform, "
        "account_id, external_id, status) VALUES ('story', 'Nesting Season', 0, 'fa', "
        "(SELECT account_id FROM accounts WHERE platform='fa'), 'NEW', 'posted')")
    desktop.commit()

    _push(desktop, server)

    assert server.execute(
        "SELECT external_id FROM publications WHERE story_name='Nesting Season'"
    ).fetchone()["external_id"] == "NEW"


# ── Deletes ───────────────────────────────────────────────────

def test_delete_is_recorded_by_the_trigger(desktop):
    desktop.execute("INSERT INTO ignored_submissions (platform, submission_id) "
                    "VALUES ('fa', '777')")
    desktop.commit()
    assert tombstones.count(desktop) == 0

    desktop.execute("DELETE FROM ignored_submissions WHERE submission_id = '777'")
    desktop.commit()

    pending = tombstones.pending(desktop)
    assert len(pending) == 1
    assert pending[0]["table"] == "ignored_submissions"
    assert pending[0]["key"] == ["fa", "777"]


def test_delete_crosses_and_removes_the_row(desktop, server):
    for conn in (desktop, server):
        conn.execute("INSERT INTO ignored_submissions (platform, submission_id) "
                     "VALUES ('fa', '777')")
        conn.commit()
    desktop.execute("DELETE FROM ignored_submissions WHERE submission_id = '777'")
    desktop.commit()

    result = _push(desktop, server)

    assert len(result["deletes"]["applied"]) == 1
    assert server.execute("SELECT COUNT(*) FROM ignored_submissions").fetchone()[0] == 0


def test_reinserting_clears_the_tombstone(desktop):
    """Delete → re-add → push must not delete upstream the row the same push
    just re-created."""
    desktop.execute("INSERT INTO inbox_state (platform, comment_id) VALUES ('fa', 'c1')")
    desktop.commit()
    desktop.execute("DELETE FROM inbox_state WHERE comment_id = 'c1'")
    desktop.commit()
    assert tombstones.count(desktop) == 1

    desktop.execute("INSERT INTO inbox_state (platform, comment_id) VALUES ('fa', 'c1')")
    desktop.commit()

    assert tombstones.count(desktop) == 0


def test_masterpiece_member_delete_is_surfaced_not_applied(desktop, server):
    """The standing rule: never delete art without showing the list first."""
    for conn in (desktop, server):
        conn.execute("INSERT INTO masterpieces (name) VALUES ('Nesting Season')")
        conn.execute("INSERT INTO masterpiece_members (masterpiece_name, platform, "
                     "submission_id) VALUES ('Nesting Season', 'fa', '12345')")
        conn.commit()
    desktop.execute("DELETE FROM masterpiece_members WHERE submission_id = '12345'")
    desktop.commit()

    result = _push(desktop, server)

    assert result["deletes"]["applied"] == []
    assert len(result["deletes"]["surfaced"]) == 1
    assert server.execute("SELECT COUNT(*) FROM masterpiece_members").fetchone()[0] == 1


def test_masterpiece_member_delete_applies_once_confirmed(desktop, server):
    for conn in (desktop, server):
        conn.execute("INSERT INTO masterpieces (name) VALUES ('Nesting Season')")
        conn.execute("INSERT INTO masterpiece_members (masterpiece_name, platform, "
                     "submission_id) VALUES ('Nesting Season', 'fa', '12345')")
        conn.commit()
    desktop.execute("DELETE FROM masterpiece_members WHERE submission_id = '12345'")
    desktop.commit()

    result = _push(desktop, server,
                   confirmed_delete_tables=("masterpiece_members",))

    assert len(result["deletes"]["applied"]) == 1
    assert server.execute("SELECT COUNT(*) FROM masterpiece_members").fetchone()[0] == 0


def test_collection_member_delete_resolves_the_collection_by_name(desktop, server):
    for conn in (desktop, server):
        cid = conn.execute("INSERT INTO collections (name) VALUES ('Launch day')").lastrowid
        conn.execute("INSERT INTO collection_members (collection_id, member_type, member_ref) "
                     "VALUES (?, 'work', 'artwork:Nesting Season')", (cid,))
        conn.commit()
    # Give the server a different collection id so a literal copy would miss.
    desktop.execute("DELETE FROM collection_members")
    desktop.commit()

    result = _push(desktop, server)

    assert len(result["deletes"]["applied"]) == 1
    assert server.execute("SELECT COUNT(*) FROM collection_members").fetchone()[0] == 0


def test_a_tombstone_for_a_deleted_collection_is_dropped(desktop, server):
    """Deleting a collection cascades to its members. That is not a delete this
    channel propagates, so the orphaned member tombstones go nowhere."""
    cid = desktop.execute("INSERT INTO collections (name) VALUES ('Gone')").lastrowid
    desktop.execute("INSERT INTO collection_members (collection_id, member_type, member_ref) "
                    "VALUES (?, 'work', 'artwork:X')", (cid,))
    desktop.commit()
    desktop.execute("DELETE FROM collections WHERE id = ?", (cid,))
    desktop.commit()

    exported = shr.export_tombstones(desktop)

    assert exported == []


def test_the_outbox_only_clears_what_the_far_side_took(desktop, server):
    """A surfaced delete must stay queued, or confirming it later is impossible."""
    for conn in (desktop, server):
        conn.execute("INSERT INTO masterpieces (name) VALUES ('M')")
        conn.execute("INSERT INTO masterpiece_members (masterpiece_name, platform, "
                     "submission_id) VALUES ('M', 'fa', '1')")
        conn.execute("INSERT INTO ignored_submissions (platform, submission_id) "
                     "VALUES ('fa', '2')")
        conn.commit()
    desktop.execute("DELETE FROM masterpiece_members")
    desktop.execute("DELETE FROM ignored_submissions")
    desktop.commit()
    assert tombstones.count(desktop) == 2

    result = _push(desktop, server)
    tombstones.clear(desktop, result["deletes"]["applied"])

    remaining = tombstones.pending(desktop)
    assert [r["table"] for r in remaining] == ["masterpiece_members"]


# ── Refusals ──────────────────────────────────────────────────

def test_a_bundle_carrying_the_queue_is_refused(server):
    bundle = {"version": shr.BUNDLE_VERSION, "tables": {"posting_queue": [{"x": 1}]},
              "tombstones": []}
    with pytest.raises(ValueError, match="HANDOFF"):
        shr.apply_bundle(server, bundle)


def test_a_bundle_carrying_an_unregistered_table_is_refused(server):
    bundle = {"version": shr.BUNDLE_VERSION, "tables": {"mystery_table": []},
              "tombstones": []}
    with pytest.raises(registry.UnregisteredTable):
        shr.apply_bundle(server, bundle)


def test_a_bundle_carrying_session_cache_is_refused(server):
    bundle = {"version": shr.BUNDLE_VERSION, "tables": {"session_cache": [{"sid": "x"}]},
              "tombstones": []}
    with pytest.raises(ValueError, match="LOC"):
        shr.apply_bundle(server, bundle)


def test_an_unknown_bundle_version_is_refused(server):
    with pytest.raises(ValueError, match="version"):
        shr.apply_bundle(server, {"version": 99, "tables": {}, "tombstones": []})


def test_the_export_never_contains_the_queue_or_the_session_cache(desktop):
    bundle = shr.export_bundle(desktop)
    for table in ("posting_queue", "posting_log", "session_cache", "share_tokens",
                  "pp_meta", "image_hashes"):
        assert table not in bundle["tables"]


def test_a_failed_apply_leaves_nothing_behind(desktop, server, monkeypatch):
    """One transaction: a bundle lands whole or not at all."""
    desktop.execute("INSERT INTO personas (name) VALUES ('Kit')")
    desktop.execute("INSERT INTO tags (name) VALUES ('feral')")
    desktop.commit()
    bundle = shr.export_bundle(desktop)

    def boom(conn, rows, ctx):
        raise sqlite3.OperationalError("simulated failure")

    monkeypatch.setitem(shr._APPLIERS, "tags", boom)
    with pytest.raises(sqlite3.OperationalError):
        shr.apply_bundle(server, bundle)

    assert server.execute("SELECT COUNT(*) FROM personas").fetchone()[0] == 0


# ── Ambiguity ─────────────────────────────────────────────────

def test_an_ambiguous_collection_name_is_skipped_and_reported(desktop, server):
    """`collections.name` is not UNIQUE. Guessing which of two same-named
    collections was meant would silently move somebody's members."""
    server.execute("INSERT INTO collections (name) VALUES ('Untitled')")
    server.execute("INSERT INTO collections (name) VALUES ('Untitled')")
    server.commit()
    desktop.execute("INSERT INTO collections (name, notes) VALUES ('Untitled', 'mine')")
    desktop.commit()

    result = _push(desktop, server)

    assert any(s["table"] == "collections" for s in result["skipped"])
    assert server.execute(
        "SELECT COUNT(*) FROM collections WHERE notes = 'mine'").fetchone()[0] == 0


# ── Content that should simply arrive ─────────────────────────

def test_a_full_round_trip_carries_the_shared_content(desktop, server):
    _account(desktop, "fa", "secondfur", default=True)
    desktop.execute("INSERT INTO personas (name, color) VALUES ('Kit', '#abc')")
    desktop.execute("INSERT INTO tags (name, color) VALUES ('feral', '#0f0')")
    desktop.execute("INSERT INTO submission_tags (tag_id, platform, submission_id) "
                    "VALUES ((SELECT tag_id FROM tags WHERE name='feral'), 'fa', 42)")
    desktop.execute("INSERT INTO submission_groups (name, description) "
                    "VALUES ('Series A', 'the arc')")
    desktop.execute("INSERT INTO submission_group_members (group_id, platform, submission_id) "
                    "VALUES ((SELECT group_id FROM submission_groups), 'fa', 42)")
    desktop.execute("INSERT INTO masterpieces (name, status) VALUES ('Nesting Season', 'junk')")
    desktop.execute("INSERT INTO masterpiece_not_duplicate (name_a, name_b) VALUES ('A', 'B')")
    desktop.execute("INSERT INTO commissions (client_name, created_at, price, status) "
                    "VALUES ('Rin', '2026-08-01 09:00:00', 120.0, 'wip')")
    desktop.execute("INSERT INTO goals (platform, scope, metric, target_value) "
                    "VALUES ('fa', 'account', 'watchers', 500)")
    desktop.execute("INSERT INTO post_contacts (name, handle_bsky) VALUES ('Rin', 'rin.bsky.social')")
    desktop.execute("INSERT INTO inbox_state (platform, comment_id, handled_at) "
                    "VALUES ('fa', 'c9', '2026-08-19 08:00:00')")
    desktop.commit()

    result = _push(desktop, server)
    assert result["rows"] > 0

    assert server.execute("SELECT color FROM personas WHERE name='Kit'").fetchone()["color"] == "#abc"
    assert server.execute("SELECT color FROM tags WHERE name='feral'").fetchone()["color"] == "#0f0"
    assert server.execute("SELECT COUNT(*) FROM submission_tags").fetchone()[0] == 1
    assert server.execute("SELECT description FROM submission_groups").fetchone()["description"] == "the arc"
    assert server.execute("SELECT COUNT(*) FROM submission_group_members").fetchone()[0] == 1
    assert server.execute(
        "SELECT status FROM masterpieces WHERE name='Nesting Season'").fetchone()["status"] == "junk"
    assert server.execute("SELECT COUNT(*) FROM masterpiece_not_duplicate").fetchone()[0] == 1
    assert server.execute("SELECT status FROM commissions").fetchone()["status"] == "wip"
    assert server.execute("SELECT target_value FROM goals").fetchone()["target_value"] == 500
    assert server.execute(
        "SELECT handle_bsky FROM post_contacts").fetchone()["handle_bsky"] == "rin.bsky.social"
    assert server.execute("SELECT handled_at FROM inbox_state").fetchone()["handled_at"] \
        == "2026-08-19 08:00:00"


def test_junk_status_crosses(desktop, server):
    """`junk` is the reversible kept-but-hidden state this project uses instead
    of deleting. Hiding a piece on one box must hide it on the other."""
    for conn in (desktop, server):
        conn.execute("INSERT INTO masterpieces (name) VALUES ('Piece')")
        conn.commit()
    desktop.execute("UPDATE masterpieces SET status = 'junk' WHERE name = 'Piece'")
    desktop.commit()

    _push(desktop, server)

    assert server.execute(
        "SELECT status FROM masterpieces WHERE name='Piece'").fetchone()["status"] == "junk"


def test_the_lazy_variant_table_is_created_on_arrival(desktop, server):
    """masterpiece_not_variant is created on first use by variant_suggest.py, so
    it can be absent on the receiver."""
    desktop.execute("CREATE TABLE IF NOT EXISTS masterpiece_not_variant ("
                    "  name_a TEXT NOT NULL, name_b TEXT NOT NULL,"
                    "  PRIMARY KEY (name_a, name_b))")
    desktop.execute("INSERT INTO masterpiece_not_variant (name_a, name_b) VALUES ('A', 'B')")
    desktop.commit()

    _push(desktop, server)

    assert server.execute("SELECT COUNT(*) FROM masterpiece_not_variant").fetchone()[0] == 1


def test_an_empty_install_pushes_an_empty_bundle(desktop, server):
    result = _push(desktop, server)
    assert result["rows"] == 0
    assert result["deletes"]["applied"] == []


def test_a_failure_after_the_deletes_still_rolls_them_back(desktop, server, monkeypatch):
    """The delete phase runs inside the same transaction as the rows.

    Worth its own test because the receiving side clears its own tombstones
    there, and the obvious way to write that clear commits — which would let a
    bundle land half-applied while reporting a failure.
    """
    for conn in (desktop, server):
        conn.execute("INSERT INTO ignored_submissions (platform, submission_id) "
                     "VALUES ('fa', '777')")
        conn.commit()
    desktop.execute("DELETE FROM ignored_submissions WHERE submission_id = '777'")
    desktop.commit()
    bundle = shr.export_bundle(desktop)

    # Raise from the outbox clear, which runs after the DELETEs have executed
    # and before the commit — the only window in which a delete could survive a
    # reported failure.
    def boom(*a, **kw):
        raise sqlite3.OperationalError("simulated failure after the deletes")
    monkeypatch.setattr(shr.tombstones, "clear", boom)

    with pytest.raises(sqlite3.OperationalError):
        shr.apply_bundle(server, bundle)

    assert server.execute("SELECT COUNT(*) FROM ignored_submissions").fetchone()[0] == 1


def test_the_receivers_own_outbox_does_not_fill_up_from_applying(desktop, server):
    """Applying a delete fires the receiver's DELETE trigger too. Left alone,
    the outbox count stops meaning anything as a diagnostic."""
    for conn in (desktop, server):
        cid = conn.execute("INSERT INTO collections (name) VALUES ('C')").lastrowid
        conn.execute("INSERT INTO collection_members (collection_id, member_type, member_ref) "
                     "VALUES (?, 'work', 'artwork:X')", (cid,))
        conn.execute("INSERT INTO ignored_submissions (platform, submission_id) "
                     "VALUES ('fa', '9')")
        conn.commit()
    # Give the server a second collection first so its ids differ from the
    # desktop's — the clear has to use the LOCAL key, not the one that arrived.
    server.execute("INSERT INTO collections (name) VALUES ('spacer')")
    server.commit()

    desktop.execute("DELETE FROM collection_members")
    desktop.execute("DELETE FROM ignored_submissions")
    desktop.commit()

    _push(desktop, server)

    assert tombstones.count(server) == 0
