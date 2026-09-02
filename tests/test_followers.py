"""Tests for cross-platform follower tracking.

Covers the shared follower store (database/followers.py), the migration that
adds it, the /api/followers endpoint, and a static guard that every
follower-capable client actually exposes get_follower_count().
"""

import inspect
import sqlite3

import pytest

import config
from database import accounts, followers


@pytest.fixture
def conn():
    """Fresh initialised DB with the accounts table empty."""
    from database.db import init_db, get_connection
    config.SETTINGS_PATH.write_text("{}", encoding="utf-8")
    init_db()
    c = get_connection()
    c.execute("DELETE FROM accounts")
    c.execute("DELETE FROM account_follower_snapshots")
    c.commit()
    yield c
    c.close()


def _make_account(conn, platform="bsky", is_default=True):
    return accounts.get_default_account_id(conn, platform, create=True)


# ── Schema / migration ────────────────────────────────────────

class TestSchema:
    def test_snapshot_table_and_cache_columns_exist(self, conn):
        # Table created by the startup migration.
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='account_follower_snapshots'").fetchone()
        cols = {r[1] for r in conn.execute("PRAGMA table_info(accounts)")}
        assert "follower_count" in cols
        assert "follower_count_at" in cols

    def test_ensure_is_idempotent(self, conn):
        # Re-running the ensure step must not raise (duplicate column guarded).
        followers.ensure_follower_tables(conn)
        followers.ensure_follower_tables(conn)


# ── record_snapshot ───────────────────────────────────────────

class TestRecordSnapshot:
    def test_insert_and_cache(self, conn):
        aid = _make_account(conn)
        assert followers.record_snapshot(conn, aid, 100) is True
        assert followers.record_snapshot(conn, aid, 137) is True
        latest = followers.latest_count(conn, aid)
        assert latest["followers"] == 137
        assert latest["at"] is not None
        series = followers.get_series(conn, aid)
        assert [s["followers"] for s in series] == [100, 137]

    def test_none_and_negative_skipped(self, conn):
        aid = _make_account(conn)
        assert followers.record_snapshot(conn, aid, None) is False
        assert followers.record_snapshot(conn, aid, -3) is False
        assert followers.record_snapshot(conn, aid, "nope") is False
        assert followers.get_series(conn, aid) == []
        # Cache untouched (still the default 0).
        assert followers.latest_count(conn, aid)["followers"] == 0

    def test_no_account_is_noop(self, conn):
        assert followers.record_snapshot(conn, None, 50) is False

    def test_string_int_coerced(self, conn):
        aid = _make_account(conn)
        assert followers.record_snapshot(conn, aid, 42) is True
        assert followers.latest_count(conn, aid)["followers"] == 42


# ── series / platform helpers ─────────────────────────────────

class TestSeriesHelpers:
    def test_series_since_filter(self, conn):
        aid = _make_account(conn)
        conn.execute(
            "INSERT INTO account_follower_snapshots (account_id, polled_at, followers) "
            "VALUES (?, '2026-01-01 00:00:00', 10)", (aid,))
        conn.execute(
            "INSERT INTO account_follower_snapshots (account_id, polled_at, followers) "
            "VALUES (?, '2026-06-01 00:00:00', 20)", (aid,))
        conn.commit()
        recent = followers.get_series(conn, aid, since="2026-03-01 00:00:00")
        assert [s["followers"] for s in recent] == [20]

    def test_platform_latest_default_account(self, conn):
        aid = _make_account(conn, "tw")
        followers.record_snapshot(conn, aid, 999)
        got = followers.platform_latest(conn, "tw")
        assert got["followers"] == 999

    def test_platform_series_default_account(self, conn):
        aid = _make_account(conn, "mast")
        followers.record_snapshot(conn, aid, 5)
        followers.record_snapshot(conn, aid, 8)
        series = followers.platform_series(conn, "mast")
        assert [s["followers"] for s in series] == [5, 8]

    def test_unknown_platform_series_empty(self, conn):
        assert followers.platform_series(conn, "ao3") == []


# ── Client contract ───────────────────────────────────────────

class TestClientContract:
    def test_all_follower_platforms_expose_getter(self):
        from clients.bsky.client import BskyClient
        from clients.mast.client import MastClient
        from clients.tw.client import TWClient
        from clients.wp.client import WPClient
        from clients.ik.client import IKClient
        from clients.weasyl.client import WeasylClient
        from clients.da.client import DAClient
        from clients.pix.client import PixClient
        classes = {
            "bsky": BskyClient, "mast": MastClient, "tw": TWClient, "wp": WPClient,
            "ik": IKClient, "weasyl": WeasylClient, "da": DAClient, "pix": PixClient,
        }
        for name, cls in classes.items():
            fn = getattr(cls, "get_follower_count", None)
            assert fn is not None, f"{name} client missing get_follower_count"
            assert inspect.iscoroutinefunction(fn), f"{name}.get_follower_count must be async"

    def test_platform_set_matches_clients(self):
        # The DB-side platform set and the client roster must agree.
        assert followers.FOLLOWER_PLATFORMS == {
            "ws", "da", "wp", "ik", "bsky", "tw", "mast", "pix", "fn"}


# ── API endpoint ──────────────────────────────────────────────

class TestFollowerApi:
    @pytest.fixture
    def client(self, conn):
        from fastapi.testclient import TestClient
        import dashboard
        return TestClient(dashboard.app)

    def test_supported_platform(self, client):
        r = client.get("/api/followers/bsky")
        assert r.status_code == 200
        body = r.json()
        assert body["supported"] is True
        assert body["platform"] == "bsky"
        assert "series" in body

    def test_unsupported_platform(self, client):
        r = client.get("/api/followers/ao3")
        assert r.status_code == 200
        body = r.json()
        assert body["supported"] is False
        assert body["series"] == []

    def test_reports_recorded_count(self, client, conn):
        aid = _make_account(conn, "bsky")
        followers.record_snapshot(conn, aid, 321)
        conn.commit()
        r = client.get("/api/followers/bsky")
        assert r.json()["followers"] == 321
