"""Tests for the mirror table registry (Stage 3's allow-list).

The registry's only job is to fail closed, so these tests are almost all about
what it refuses. The one that earns its keep is
``test_every_live_table_is_registered``: it turns "somebody added a table and
never classified it" into a red test rather than a table that quietly does or
does not cross machines depending on which default it fell into.
"""
from __future__ import annotations

import pytest

from mirror import registry, tombstones


@pytest.fixture
def conn(tmp_path, monkeypatch):
    import config
    from database import db as db_mod
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "pawpoller.db")
    db_mod.init_db()
    c = db_mod.get_connection()
    yield c
    c.close()


def test_every_live_table_is_registered(conn):
    """A freshly initialised database must contain nothing the registry has not
    classified. This is the fail-closed guarantee, and it is a test rather than
    a runtime check because the right time to notice is when the table is added.
    """
    audit = registry.audit(conn)
    assert audit["unregistered"] == [], (
        f"Unclassified tables: {audit['unregistered']}. Add them to "
        f"mirror/registry.py with a reason before they can cross machines."
    )


def test_class_counts_match_the_spec(conn):
    counts = registry.audit(conn)["counts"]
    # 19 platforms x (submissions, snapshots, poll_log) = 57, plus the 11
    # cross-platform telemetry tables the spec enumerates.
    # +2 in 4.0.10: tg_snapshots and tg_poll_log. Telegram cannot join the
    # PLATFORM_PREFIXES loop that generates the other trios, because its
    # submissions table is SHR rather than SRV - PawPoller sends every
    # Telegram post itself, so unlike a polled platform the DESKTOP can
    # originate one. See the registry entries for the full reasoning.
    assert counts["SRV"] == 70
    # The spec's §1 says 25 but enumerates 26; two of those (posting_queue,
    # posting_log) are reclassified HANDOFF here for the reasons in the module
    # docstring, leaving 24 that actually travel as shared rows.
    # +2 in 3.10.0: the artist registry (`artists`, `artist_handles`) is shared
    # reference data the user maintains, and both are already naturally keyed.
    # +1 in 4.0.10: tg_submissions — the only *_submissions table the DESKTOP
    # can originate, because PawPoller writes it when it posts rather than
    # learning it from a poll.
    assert counts["SHR"] == 27
    assert counts["HANDOFF"] == 2
    assert counts["DER"] == 1
    assert counts["LOC"] == 5  # + this stage's own outbox


def test_unregistered_table_raises_rather_than_defaulting():
    with pytest.raises(registry.UnregisteredTable):
        registry.rule_for("some_table_nobody_classified")


def test_the_queue_is_not_a_shared_table():
    """§0.2: both installs run the scheduler, so a shared queue double-posts."""
    for table in ("posting_queue", "posting_log"):
        assert registry.rule_for(table).ownership == registry.HANDOFF
        assert not registry.rule_for(table).syncs_upward


def test_local_only_tables_never_travel():
    for table in ("session_cache", "share_tokens", "pp_meta", "sqlite_sequence",
                  "mirror_tombstones"):
        assert registry.rule_for(table).ownership == registry.LOC
        assert not registry.rule_for(table).syncs_upward


def test_no_shr_key_contains_a_surrogate_id():
    """§D2. A key naming pub_id/post_id/account_id/tag_id would be a surrogate
    crossing the wire under a different name."""
    banned = {"pub_id", "post_id", "account_id", "tag_id", "group_id",
              "collection_id", "link_id", "persona_id", "goal_id", "id"}
    for name in registry.SHR_ORDER:
        rule = registry.rule_for(name)
        assert not (set(rule.key) & banned), f"{name} keys on a surrogate: {rule.key}"


def test_shr_order_covers_exactly_the_shared_tables():
    assert set(registry.SHR_ORDER) == set(registry.tables_in_class(registry.SHR))
    assert len(registry.SHR_ORDER) == len(set(registry.SHR_ORDER))


def test_tombstone_registry_and_triggers_agree():
    """A table whose deletes the registry claims to carry, but which has no
    trigger, silently drops every delete."""
    trigger_tables = {t for t, _ in tombstones._TOMBSTONED}
    assert set(registry.TOMBSTONE_TABLES) == trigger_tables


def test_tombstone_key_columns_match_the_export_key():
    """The trigger writes the natural key; the far side matches on it. If the
    two disagree the delete is recorded under a name nothing can find.

    ``collection_members`` is the deliberate exception — the trigger records
    ``collection_id`` because ON DELETE CASCADE leaves no Python frame to
    resolve the name, and ``export_tombstones`` resolves it on the way out.
    """
    for table, columns in tombstones._TOMBSTONED:
        if table == "collection_members":
            continue
        assert columns == registry.rule_for(table).key, table


def test_masterpiece_member_deletes_are_surfaced_not_applied():
    """The standing project rule: nothing in the neighbourhood of removing art
    propagates without the operator seeing the list first."""
    assert registry.rule_for("masterpiece_members").deletes == registry.SURFACE
    assert "masterpiece_members" not in registry.AUTO_DELETE_TABLES


def test_publications_are_insert_only_upward():
    for table in ("publications", "post_publications"):
        assert registry.rule_for(table).upward == registry.INSERT_ONLY


def test_every_rule_carries_a_reason():
    for name, rule in registry.REGISTRY.items():
        assert rule.reason.strip(), f"{name} has no reason recorded"
