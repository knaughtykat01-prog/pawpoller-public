"""Tests for the personas layer (cross-platform account grouping).

Covers the migration (personas table + accounts.persona_id), the CRUD/assign
logic in database/personas.py, persona_stats summing, and manifest round-trip.
"""

import sqlite3

import pytest

import config
from database import accounts, personas


@pytest.fixture
def conn():
    """Fresh DB with personas + accounts empty and settings reset to {}."""
    from database.db import init_db, get_connection
    config.SETTINGS_PATH.write_text("{}", encoding="utf-8")
    init_db()
    c = get_connection()
    for t in ("personas", "accounts", "publications", "posting_queue",
              "posting_log", "submissions", "fa_submissions"):
        try:
            c.execute(f"DELETE FROM {t}")
        except sqlite3.OperationalError:
            pass
    c.commit()
    yield c
    c.close()


# ── Migration ──────────────────────────────────────────────────

def test_migration_creates_table_and_column(conn):
    tbls = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert "personas" in tbls
    cols = {r[1] for r in conn.execute("PRAGMA table_info(accounts)").fetchall()}
    assert "persona_id" in cols
    # New accounts default to NULL persona (Unassigned).
    aid = accounts.create_account(conn, "ib", "main")
    assert accounts.get_account(conn, aid)["persona_id"] is None


def test_migration_idempotent(conn):
    """Re-running init_db must not raise and must keep the column."""
    from database.db import init_db
    init_db()
    cols = {r[1] for r in conn.execute("PRAGMA table_info(accounts)").fetchall()}
    assert "persona_id" in cols


# ── CRUD + assignment ──────────────────────────────────────────

def test_crud(conn):
    pid = personas.create_persona(conn, "Alpha", color="#123456")
    p = personas.get_persona(conn, pid)
    assert p["name"] == "Alpha" and p["color"] == "#123456"
    personas.update_persona(conn, pid, name="Beta")
    assert personas.get_persona(conn, pid)["name"] == "Beta"
    assert any(x["persona_id"] == pid for x in personas.list_personas(conn))
    assert personas.delete_persona(conn, pid) is True
    assert personas.get_persona(conn, pid) is None


def test_assign_and_unassign(conn):
    pid = personas.create_persona(conn, "P")
    aid = accounts.create_account(conn, "ib", "i")
    personas.assign_account_persona(conn, aid, pid)
    assert accounts.get_account(conn, aid)["persona_id"] == pid
    # None unassigns (update_account can't, hence the dedicated function).
    personas.assign_account_persona(conn, aid, None)
    assert accounts.get_account(conn, aid)["persona_id"] is None


def test_delete_persona_unassigns_accounts(conn):
    pid = personas.create_persona(conn, "P")
    aid = accounts.create_account(conn, "fa", "f")
    personas.assign_account_persona(conn, aid, pid)
    assert accounts.get_account(conn, aid)["persona_id"] == pid
    personas.delete_persona(conn, pid)
    # Account survives but falls back to Unassigned — no orphan reference.
    assert accounts.get_account(conn, aid)["persona_id"] is None


def test_list_accounts_by_persona(conn):
    pid = personas.create_persona(conn, "Kithe")
    a1 = accounts.create_account(conn, "ib", "ib-main")
    a2 = accounts.create_account(conn, "fa", "fa-main")
    a3 = accounts.create_account(conn, "ws", "ws-loose")  # unassigned
    personas.assign_account_persona(conn, a1, pid)
    personas.assign_account_persona(conn, a2, pid)
    groups = personas.list_accounts_by_persona(conn)
    assert {a["account_id"] for a in groups[pid]} == {a1, a2}
    assert {a["account_id"] for a in groups[None]} == {a3}


# ── persona_stats summing ──────────────────────────────────────

def test_persona_stats_sums_member_accounts(conn, monkeypatch):
    pid = personas.create_persona(conn, "Kithe")
    a1 = accounts.create_account(conn, "ib", "ib-main")
    a2 = accounts.create_account(conn, "fa", "fa-main")
    a3 = accounts.create_account(conn, "ws", "ws-other")  # NOT in persona
    personas.assign_account_persona(conn, a1, pid)
    personas.assign_account_persona(conn, a2, pid)
    fake = {
        a1: {"submissions": 2, "views": 100, "favorites": 10, "comments": 1},
        a2: {"submissions": 3, "views": 50, "favorites": 5, "comments": 2},
        a3: {"submissions": 9, "views": 999, "favorites": 99, "comments": 9},
    }
    # personas.persona_stats calls accounts.account_stats via the module attr.
    monkeypatch.setattr(accounts, "account_stats", lambda c, aid, plat: fake.get(aid))
    st = personas.persona_stats(conn, pid)
    assert st["combined"] == {"submissions": 5, "views": 150, "favorites": 15,
                              "comments": 3, "score": 0}
    assert set(st["by_platform"]) == {"ib", "fa"}  # a3's "ws" excluded
    assert st["by_platform"]["ib"]["views"] == 100


# ── Sync manifest round-trip ───────────────────────────────────

def test_manifest_roundtrip_preserves_assignment(conn):
    pid = personas.create_persona(conn, "Kithe", color="#ff0000")
    aid = accounts.create_account(conn, "ib", "main")
    personas.assign_account_persona(conn, aid, pid)
    pman = personas.get_manifest(conn)
    aman = accounts.get_manifest(conn)
    # Wipe and re-materialise from the manifests (additive upsert).
    conn.execute("DELETE FROM accounts")
    conn.execute("DELETE FROM personas")
    conn.commit()
    personas.apply_manifest(conn, pman)
    accounts.apply_manifest(conn, aman)
    p = personas.get_persona(conn, pid)
    assert p and p["name"] == "Kithe" and p["color"] == "#ff0000"
    assert accounts.get_account(conn, aid)["persona_id"] == pid


def test_manifest_absent_persona_id_does_not_clobber(conn):
    """An old-client accounts manifest (no persona_id key) must not wipe a local
    assignment."""
    pid = personas.create_persona(conn, "Kithe")
    aid = accounts.create_account(conn, "ib", "main")
    personas.assign_account_persona(conn, aid, pid)
    # Simulate an old client: manifest entry omits persona_id entirely.
    old_manifest = [{
        "account_id": aid, "platform": "ib", "label": "main", "handle": "",
        "enabled": 1, "is_default": 0, "sort_order": 0,
    }]
    accounts.apply_manifest(conn, old_manifest)
    assert accounts.get_account(conn, aid)["persona_id"] == pid  # unchanged


# ── Phase 2: per-account read scoping (Inkbunny) ───────────────

def test_ib_summary_and_submissions_scope_by_account(conn):
    """get_summary / get_all_submissions filter to one account when given an
    account_id, and aggregate across accounts when not (account_id=None)."""
    from database import queries
    a1 = accounts.create_account(conn, "ib", "ib-a1")
    a2 = accounts.create_account(conn, "ib", "ib-a2")
    queries.upsert_submission(conn, {"submission_id": 101, "title": "A1-one", "views": 100, "favorites_count": 5, "comments_count": 1}, a1)
    queries.upsert_submission(conn, {"submission_id": 102, "title": "A1-two", "views": 50, "favorites_count": 2, "comments_count": 0}, a1)
    queries.upsert_submission(conn, {"submission_id": 201, "title": "A2-one", "views": 999, "favorites_count": 9, "comments_count": 3}, a2)
    conn.commit()

    s1 = queries.get_summary(conn, account_id=a1)
    assert s1["total_submissions"] == 2
    assert s1["total_views"] == 150
    assert s1["total_favorites"] == 7
    assert [t["submission_id"] for t in s1["top_viewed"]] == [101, 102]

    s2 = queries.get_summary(conn, account_id=a2)
    assert s2["total_submissions"] == 1 and s2["total_views"] == 999

    s_all = queries.get_summary(conn)  # All accounts
    assert s_all["total_submissions"] == 3

    assert {x["submission_id"] for x in queries.get_all_submissions(conn, account_id=a1)} == {101, 102}
    assert {x["submission_id"] for x in queries.get_all_submissions(conn)} == {101, 102, 201}
