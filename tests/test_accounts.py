"""Tests for the multi-account foundation (Phase 0).

Covers the credential resolver + vault routing in config.py and the accounts
registry/CRUD/seeding/manifest in database/accounts.py.
"""

import sqlite3

import pytest

import config
from database import accounts


@pytest.fixture
def conn():
    """Fresh DB with the accounts table empty and settings reset to {}."""
    from database.db import init_db, get_connection
    config.SETTINGS_PATH.write_text("{}", encoding="utf-8")
    init_db()
    c = get_connection()
    for t in ("accounts", "publications", "posting_queue", "posting_log",
              "submissions", "fa_submissions"):
        c.execute(f"DELETE FROM {t}")
    c.commit()
    yield c
    c.close()


# ── Credential key classification (vault routing) ──────────────

class TestIsCredentialKey:
    def test_legacy_secret_keys(self):
        assert config.is_credential_key("password")
        assert config.is_credential_key("fa_cookie_a")
        assert config.is_credential_key("bsky_app_password")

    def test_non_secret_identity_keys(self):
        # fa_username is identity, not a secret — stays in plaintext.
        assert not config.is_credential_key("fa_username")
        assert not config.is_credential_key("polling_paused")
        assert not config.is_credential_key("setup_mode")

    def test_namespaced_secret_keys(self):
        assert config.is_credential_key("acct_5_password")
        assert config.is_credential_key("acct_12_fa_cookie_a")

    def test_namespaced_non_secret_keys(self):
        # Mirrors the default account: namespaced identity stays plaintext.
        assert not config.is_credential_key("acct_5_fa_username")


# ── Key naming + resolver ──────────────────────────────────────

class TestAccountSettingKey:
    def test_default_uses_bare_field(self):
        assert config.account_setting_key(1, "password", is_default=True) == "password"

    def test_non_default_is_namespaced(self):
        assert config.account_setting_key(7, "password", is_default=False) == "acct_7_password"


class TestResolveCredentials:
    def test_default_account_reads_flat_keys(self):
        settings = {"username": "kit", "password": "hunter2"}
        creds = config.resolve_account_credentials("ib", 1, True, settings)
        assert creds == {"username": "kit", "password": "hunter2"}

    def test_extra_account_reads_namespaced_keys(self):
        settings = {
            "username": "main", "password": "mainpw",
            "acct_7_username": "alt", "acct_7_password": "altpw",
        }
        creds = config.resolve_account_credentials("ib", 7, False, settings)
        assert creds == {"username": "alt", "password": "altpw"}

    def test_fa_fields(self):
        settings = {"fa_username": "fox", "fa_cookie_a": "aaa", "fa_cookie_b": "bbb"}
        creds = config.resolve_account_credentials("fa", 1, True, settings)
        assert creds == {"fa_username": "fox", "fa_cookie_a": "aaa", "fa_cookie_b": "bbb"}

    def test_missing_fields_default_blank(self):
        creds = config.resolve_account_credentials("ib", 9, False, {})
        assert creds == {"username": "", "password": ""}


# ── Accounts table + seeding ───────────────────────────────────

class TestAccountsRegistry:
    def test_table_and_indexes_exist(self, conn):
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert "accounts" in tables

    def test_seed_creates_defaults_only_for_configured_platforms(self, conn):
        settings = {"username": "kit", "password": "pw",
                    "fa_username": "fox", "fa_cookie_a": "aaa"}
        created = accounts.seed_default_accounts(conn, settings)
        conn.commit()
        assert created == 2  # ib + fa only
        rows = accounts.list_accounts(conn)
        platforms = {r["platform"] for r in rows}
        assert platforms == {"ib", "fa"}
        assert all(r["is_default"] == 1 for r in rows)

    def test_seed_is_idempotent(self, conn):
        settings = {"username": "kit", "password": "pw"}
        accounts.seed_default_accounts(conn, settings)
        conn.commit()
        again = accounts.seed_default_accounts(conn, settings)
        conn.commit()
        assert again == 0
        assert accounts.count_accounts(conn, "ib") == 1

    def test_get_default_account_id_creates_on_demand(self, conn):
        assert accounts.get_default_account_id(conn, "ib") is None
        aid = accounts.get_default_account_id(conn, "ib", create=True, settings={"username": "kit"})
        conn.commit()
        assert aid is not None
        assert accounts.get_default_account_id(conn, "ib") == aid

    def test_get_default_account_id_self_commits(self, conn):
        """create=True must persist even when the caller never commits.

        Regression: the pollers and the server poll-loop seed call this and then
        close their connection WITHOUT committing. Before the fix the INSERT
        rolled back on close, leaving platforms with creds (tw/bsky) but no
        account row — so they never appeared on the Accounts page or got polled.
        """
        from database.db import get_connection
        writer = get_connection()
        try:
            aid = accounts.get_default_account_id(
                writer, "tw", create=True, settings={"tw_target_user": "kit"})
            assert aid is not None
        finally:
            writer.close()  # deliberately NO writer.commit()
        # A separate connection must see the row -> it was committed internally.
        assert accounts.get_default_account_id(conn, "tw") == aid

    def test_only_one_default_per_platform(self, conn):
        accounts.get_default_account_id(conn, "ib", create=True, settings={})
        conn.commit()
        # A second default for the same platform must be rejected by the index.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO accounts (platform, label, is_default) VALUES ('ib', 'dup', 1)")
            conn.commit()

    def test_account_ids_are_global_not_per_platform(self, conn):
        # Surrogate key spans platforms — defaults are NOT all id=1.
        settings = {"username": "kit", "password": "pw",
                    "fa_username": "fox", "fa_cookie_a": "aaa"}
        accounts.seed_default_accounts(conn, settings)
        conn.commit()
        ib_id = accounts.get_default_account_id(conn, "ib")
        fa_id = accounts.get_default_account_id(conn, "fa")
        assert ib_id != fa_id


# ── CRUD ───────────────────────────────────────────────────────

class TestAccountsCRUD:
    def test_create_and_list(self, conn):
        aid = accounts.create_account(conn, "ib", "Alt IB", handle="altkit")
        rows = accounts.list_accounts(conn, platform="ib")
        assert len(rows) == 1
        assert rows[0]["account_id"] == aid
        assert rows[0]["label"] == "Alt IB"
        assert rows[0]["is_default"] == 0

    def test_second_default_request_downgraded(self, conn):
        first = accounts.create_account(conn, "ib", "Main", is_default=True)
        second = accounts.create_account(conn, "ib", "Alt", is_default=True)
        assert accounts.get_account(conn, first)["is_default"] == 1
        assert accounts.get_account(conn, second)["is_default"] == 0

    def test_update(self, conn):
        aid = accounts.create_account(conn, "fa", "Old")
        assert accounts.update_account(conn, aid, label="New", enabled=False)
        row = accounts.get_account(conn, aid)
        assert row["label"] == "New"
        assert row["enabled"] == 0

    def test_delete(self, conn):
        aid = accounts.create_account(conn, "fa", "Temp")
        assert accounts.delete_account(conn, aid)
        assert accounts.get_account(conn, aid) is None


# ── Manifest round-trip ────────────────────────────────────────

class TestManifest:
    def test_export_import_roundtrip(self, conn):
        a1 = accounts.create_account(conn, "ib", "Main", is_default=True)
        a2 = accounts.create_account(conn, "ib", "Alt")
        manifest = accounts.get_manifest(conn)
        assert {m["account_id"] for m in manifest} == {a1, a2}
        # Wipe and re-apply — account_ids must be preserved.
        conn.execute("DELETE FROM accounts")
        conn.commit()
        n = accounts.apply_manifest(conn, manifest)
        assert n == 2
        restored = {m["account_id"]: m for m in accounts.get_manifest(conn)}
        assert restored[a1]["label"] == "Main"
        assert restored[a1]["is_default"] == 1
        assert restored[a2]["label"] == "Alt"

    def test_apply_accepts_json_string(self, conn):
        accounts.create_account(conn, "fa", "X", is_default=True)
        import json
        manifest_json = json.dumps(accounts.get_manifest(conn))
        conn.execute("DELETE FROM accounts")
        conn.commit()
        assert accounts.apply_manifest(conn, manifest_json) == 1


# ── Manifest id collisions (the 2026-08-12 prod corruption) ────
#
# Both installs run seed_default_accounts at boot against their own AUTOINCREMENT
# sequence, so the same account_id routinely means a different platform on each
# box. The old ON CONFLICT(account_id) upsert wrote label/handle straight through
# without checking platform.

class TestManifestIdCollisions:
    def test_id_belonging_to_another_platform_never_writes_through(self, conn):
        """The exact incident: server id=3 is Weasyl, the desktop's id=3 is SoFurry."""
        aid = accounts.create_account(conn, "ws", "Weasyl (default)")
        incoming = [{"account_id": aid, "platform": "sf", "label": "SoFurry (default)",
                     "handle": "someone", "enabled": 1, "is_default": 0, "sort_order": 0}]
        accounts.apply_manifest(conn, incoming)
        row = accounts.get_account(conn, aid)
        assert row["platform"] == "ws"              # identity intact
        assert row["label"] == "Weasyl (default)"   # and not wearing SoFurry's name
        assert (row["handle"] or "") == ""
        assert accounts.count_accounts(conn, "sf") == 1   # arrived as its own row

    def test_the_incident_in_full(self, conn):
        """All four corrupted rows, replayed: seeded defaults offset by one.

        No handles on ws/ao3 (unconnected platforms), so (platform, handle) can't
        see them — this is the case rule 2 exists for.
        """
        ws = accounts.create_account(conn, "ws", "Weasyl (default)", is_default=True)
        sf = accounts.create_account(conn, "sf", "SoFurry (default)",
                                     handle="KnaughtyKat", is_default=True)
        sqw = accounts.create_account(conn, "sqw", "SquidgeWorld (default)",
                                      handle="KnaughtyKat", is_default=True)
        ao3 = accounts.create_account(conn, "ao3", "AO3 (default)", is_default=True)
        # The desktop's ids are shifted one earlier than the server's.
        incoming = [
            {"account_id": ws,  "platform": "sf",  "label": "SoFurry (default)",
             "handle": "KnaughtyKat", "is_default": 1},
            {"account_id": sf,  "platform": "sqw", "label": "SquidgeWorld (default)",
             "handle": "KnaughtyKat", "is_default": 1},
            {"account_id": sqw, "platform": "ao3", "label": "AO3 (default)",
             "handle": "", "is_default": 1},
            {"account_id": ao3, "platform": "wp",  "label": "Wattpad (default)",
             "handle": "", "is_default": 1},
        ]
        accounts.apply_manifest(conn, incoming)
        for aid, platform, label in ((ws, "ws", "Weasyl (default)"),
                                     (sf, "sf", "SoFurry (default)"),
                                     (sqw, "sqw", "SquidgeWorld (default)"),
                                     (ao3, "ao3", "AO3 (default)")):
            row = accounts.get_account(conn, aid)
            assert row["platform"] == platform, f"id {aid} changed platform"
            assert row["label"] == label, f"id {aid} took another platform's label"
        for platform in ("ws", "sf", "sqw", "ao3"):
            assert accounts.count_accounts(conn, platform) == 1   # no duplicates
        assert accounts.count_accounts(conn, "wp") == 1           # genuinely new

    def test_offset_ids_resolve_by_platform_and_handle(self, conn):
        """An id offset must still land the update on the RIGHT row, not skip it."""
        ws = accounts.create_account(conn, "ws", "Weasyl", handle="kat")
        sf = accounts.create_account(conn, "sf", "SoFurry", handle="kat")
        # Peer has the same two accounts, but its ids are shifted by one.
        incoming = [
            {"account_id": ws, "platform": "sf", "label": "SoFurry renamed", "handle": "kat"},
            {"account_id": sf + 5, "platform": "ws", "label": "Weasyl renamed", "handle": "kat"},
        ]
        assert accounts.apply_manifest(conn, incoming) == 2
        assert accounts.get_account(conn, sf)["label"] == "SoFurry renamed"
        assert accounts.get_account(conn, ws)["label"] == "Weasyl renamed"
        assert accounts.get_account(conn, sf)["platform"] == "sf"
        assert accounts.get_account(conn, ws)["platform"] == "ws"
        assert accounts.count_accounts(conn, "sf") == 1   # matched, not duplicated
        assert accounts.count_accounts(conn, "ws") == 1

    def test_handle_match_is_case_insensitive(self, conn):
        aid = accounts.create_account(conn, "fa", "Main", handle="KnaughtyKat")
        n = accounts.apply_manifest(conn, [
            {"account_id": aid + 9, "platform": "fa", "label": "Renamed", "handle": "knaughtykat"}])
        assert n == 1
        assert accounts.count_accounts(conn, "fa") == 1
        assert accounts.get_account(conn, aid)["label"] == "Renamed"

    def test_new_account_on_a_taken_id_is_inserted_not_dropped(self, conn):
        """A genuinely new peer account whose id is taken locally still arrives."""
        taken = accounts.create_account(conn, "ws", "Weasyl", handle="kat")
        n = accounts.apply_manifest(conn, [
            {"account_id": taken, "platform": "fa", "label": "FA", "handle": "newguy"}])
        assert n == 1
        assert accounts.count_accounts(conn, "fa") == 1
        assert accounts.get_account(conn, taken)["platform"] == "ws"   # untouched

    def test_platform_is_never_updated(self, conn):
        aid = accounts.create_account(conn, "fa", "Main", handle="kat")
        accounts.apply_manifest(conn, [
            {"account_id": aid, "platform": "fa", "label": "Main", "handle": "kat"}])
        assert accounts.get_account(conn, aid)["platform"] == "fa"

    def test_persona_lands_on_the_resolved_row_not_the_manifest_id(self, conn):
        """persona_id must follow the row we actually matched, or it tags a stranger."""
        from database import personas
        pid = personas.create_persona(conn, "Kithe")
        ws = accounts.create_account(conn, "ws", "Weasyl", handle="kat")
        fa = accounts.create_account(conn, "fa", "FA", handle="kat")
        accounts.apply_manifest(conn, [
            {"account_id": ws, "platform": "fa", "label": "FA", "handle": "kat",
             "persona_id": pid}])
        assert accounts.get_account(conn, fa)["persona_id"] == pid
        assert accounts.get_account(conn, ws)["persona_id"] is None


# ── Vault routing for namespaced secrets ───────────────────────

class TestPostingPerAccount:
    """The publications data layer keeps each account's posts separate."""

    def test_same_chapter_two_accounts(self, conn):
        from database import posting_queries
        a1 = accounts.create_account(conn, "ib", "Main", is_default=True)
        a2 = accounts.create_account(conn, "ib", "Alt")
        p1 = posting_queries.upsert_publication(
            conn, "Story", 1, "ib", account_id=a1, external_id="111", status="posted")
        p2 = posting_queries.upsert_publication(
            conn, "Story", 1, "ib", account_id=a2, external_id="222", status="posted")
        # Two distinct publications for the same (story, chapter, platform).
        assert p1 != p2
        rows = posting_queries.get_publications(conn, story_name="Story", platform="ib")
        assert len(rows) == 2
        # Each account resolves to its own row + external id.
        assert posting_queries.get_publication_by_story(conn, "Story", 1, "ib", a1)["external_id"] == "111"
        assert posting_queries.get_publication_by_story(conn, "Story", 1, "ib", a2)["external_id"] == "222"

    def test_reupsert_same_account_updates_in_place(self, conn):
        from database import posting_queries
        a1 = accounts.create_account(conn, "ib", "Main", is_default=True)
        p1 = posting_queries.upsert_publication(
            conn, "Story", 1, "ib", account_id=a1, external_id="111", status="posted")
        p1b = posting_queries.upsert_publication(
            conn, "Story", 1, "ib", account_id=a1, external_id="111", status="posted")
        assert p1 == p1b  # same row updated, not a duplicate
        assert len(posting_queries.get_publications(conn, story_name="Story")) == 1


class TestAccountStats:
    """Per-account stat rollups segregate by account_id."""

    def test_ib_stats_per_account(self, conn):
        a1 = accounts.create_account(conn, "ib", "Main", is_default=True)
        a2 = accounts.create_account(conn, "ib", "Alt")
        # Two submissions for a1, one for a2.
        conn.execute("INSERT INTO submissions (submission_id, account_id, views, favorites_count, comments_count) VALUES (1, ?, 100, 10, 2)", (a1,))
        conn.execute("INSERT INTO submissions (submission_id, account_id, views, favorites_count, comments_count) VALUES (2, ?, 50, 5, 1)", (a1,))
        conn.execute("INSERT INTO submissions (submission_id, account_id, views, favorites_count, comments_count) VALUES (3, ?, 7, 1, 0)", (a2,))
        conn.commit()
        s1 = accounts.account_stats(conn, a1, "ib")
        assert s1 == {"submissions": 2, "views": 150, "favorites": 15,
                      "comments": 3, "score": 0}
        s2 = accounts.account_stats(conn, a2, "ib")
        assert s2 == {"submissions": 1, "views": 7, "favorites": 1,
                      "comments": 0, "score": 0}

    def test_engagement_platform_favourites_are_not_dropped(self, conn):
        """Twitter stores favourites in a `likes` column. This used to sniff for
        `favorites_count` only and write 0, so every Twitter/e621/Itaku/Bluesky/
        Mastodon/Tumblr account reported zero favourites — and the "By persona"
        roll-up under-counted with it."""
        a = accounts.create_account(conn, "tw", "Main", is_default=True)
        conn.execute(
            "INSERT INTO tw_submissions (submission_id, account_id, views, likes, replies)"
            " VALUES ('1', ?, 1659, 33, 2)", (a,))
        conn.commit()
        st = accounts.account_stats(conn, a, "tw")
        assert st["views"] == 1659
        assert st["favorites"] == 33      # was 0 before the registry
        assert st["comments"] == 2

    def test_score_platform_keeps_score_out_of_views(self, conn):
        a = accounts.create_account(conn, "e621", "Main", is_default=True)
        conn.execute(
            "INSERT INTO e621_submissions (submission_id, account_id, score,"
            " favorites_count, comments_count) VALUES ('1', ?, 63, 161, 0)", (a,))
        conn.commit()
        st = accounts.account_stats(conn, a, "e621")
        assert st["views"] == 0           # e621 exposes no view count
        assert st["score"] == 63
        assert st["favorites"] == 161

    def test_unknown_platform_returns_none(self, conn):
        # account_stats gracefully returns None for a platform with no registry
        # entry (and for one whose table isn't account-aware yet).
        a = accounts.create_account(conn, "zzz", "X", is_default=True)
        assert accounts.account_stats(conn, a, "zzz") is None


class TestVaultRouting:
    def test_namespaced_secret_encrypted_in_local_mode(self, conn, tmp_path, monkeypatch):
        Fernet = pytest.importorskip("cryptography.fernet").Fernet
        key = Fernet.generate_key()
        monkeypatch.setattr(config, "_get_vault_key", lambda: key)
        monkeypatch.setattr(config, "VAULT_PATH", tmp_path / "vault.json")

        config.save_settings({"credential_mode": "local"})
        config.save_settings({"acct_5_password": "topsecret", "acct_5_fa_username": "fox"})

        # The secret must NOT be in plaintext settings.json.
        import json
        plaintext = json.loads(config.SETTINGS_PATH.read_text(encoding="utf-8"))
        assert "acct_5_password" not in plaintext
        # The non-secret identity field stays in plaintext.
        assert plaintext.get("acct_5_fa_username") == "fox"
        # But the merged view (get_settings) still resolves the secret.
        merged = config.get_settings()
        assert merged["acct_5_password"] == "topsecret"
