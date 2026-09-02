"""FurryNetwork poll cycle — full integration with a faked client.

Runs run_fn_poll_cycle end-to-end against the in-memory DB with a stub FnClient,
asserting submissions + snapshots are stored, the follower count is captured,
and the rotated tokens are persisted. No network.
"""
import asyncio

import config
from database.db import get_connection
from database import fn_queries, followers
import polling.fn_poller as fp


class _FakeFn:
    def __init__(self, **kw):
        self.username = kw.get("username", "")
        self.password = kw.get("password", "")
        self.access_token = "AT-new"
        self.refresh_token = "RT-new"

    async def close(self):
        pass

    async def validate_session(self):
        return "me@ex.com"

    async def get_all_post_uris(self):
        return [{"post_uri": "111", "raw": {}}, {"post_uri": "112", "raw": {}}]

    async def get_post_details_batch(self, items):
        return [
            {"post_uri": "111", "title": "Wolf", "username": "kit",
             "posted_at": "2020-01-01", "content_type": "image", "rating": "adult",
             "description": "d", "keywords": ["wolf"], "link": "http://l1",
             "thumbnail_url": "http://t1", "file_url": "http://f1",
             "views": 500, "favorites_count": 40, "comments_count": 3, "has_media": 1},
            {"post_uri": "112", "title": "Fox", "username": "kit",
             "posted_at": "2020-02-01", "content_type": "image", "rating": "general",
             "description": "", "keywords": [], "link": "http://l2",
             "thumbnail_url": "", "file_url": "", "views": 10,
             "favorites_count": 1, "comments_count": 0, "has_media": 0},
        ]

    async def get_follower_count(self):
        return 42


def test_poll_cycle_stores_everything(monkeypatch):
    config.save_settings({"fn_username": "me@ex.com", "fn_password": "pw"})
    monkeypatch.setattr(fp, "FnClient", _FakeFn)
    fp._fn_client = None
    fp._fn_first_poll_done.clear()

    stats = asyncio.run(fp.run_fn_poll_cycle())
    assert stats["submissions_found"] == 2
    assert stats["snapshots_inserted"] == 2

    conn = get_connection()
    try:
        subs = fn_queries.get_all_fn_submissions(conn)
        assert {s["submission_id"] for s in subs} == {"111", "112"}
        wolf = fn_queries.get_fn_submission(conn, "111")
        assert wolf["views"] == 500 and wolf["rating"] == "adult"
        assert wolf["account_id"] > 0                       # tagged to the fn account
        assert len(fn_queries.get_fn_snapshots(conn, "111")) == 1
        # Follower count captured for the fn platform.
        latest = followers.platform_latest(conn, "fn")
        assert latest and latest["followers"] == 42
        # Poll log recorded a success.
        assert fn_queries.get_fn_last_poll(conn)["status"] == "success"
    finally:
        conn.close()

    # Rotated tokens persisted for next cycle.
    s = config.get_settings()
    assert s.get("fn_refresh_token") == "RT-new"
    assert s.get("fn_access_token") == "AT-new"
