"""SCHEDTWICE (4.88.8): scheduling something already waiting for that site asks first.

Two tabs, or forgetting it was scheduled, queued a second row and the site got it twice.
The schedule routes answer 409 {already_scheduled} unless the request says `again`;
the page asks "Schedule it again anyway?" (API._schedule).
"""
from __future__ import annotations

import os
import tempfile

import pytest


@pytest.fixture()
def conn(monkeypatch):
    import config
    from database import db as dbm
    monkeypatch.setattr(config, "DB_PATH", os.path.join(tempfile.mkdtemp(), "pp.db"))
    dbm.init_db()
    c = dbm.get_connection()
    yield c
    c.close()


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient
    import dashboard
    return TestClient(dashboard.app, client=("127.0.0.1", 50000))   # local: scheduling is owner-only


def test_pending_post_at_matches_only_the_same_thing_site_and_account(conn):
    from database import accounts, posting_queries as q
    main = accounts.create_account(conn, "ws", "WS main", is_default=True)
    alt = accounts.create_account(conn, "ws", "WS alt")
    conn.commit()
    q.add_to_queue(conn, "Sample_Story", 2, "ws", scheduled_at="2099-01-01 10:00:00")
    assert q.pending_post_at(conn, "story", "Sample_Story", 2, "ws") == "2099-01-01 10:00:00"
    assert q.pending_post_at(conn, "story", "Sample_Story", 2, "ws", main) == "2099-01-01 10:00:00"   # None = default
    assert q.pending_post_at(conn, "story", "Sample_Story", 2, "ws", alt) is None
    assert q.pending_post_at(conn, "story", "Sample_Story", 3, "ws") is None
    assert q.pending_post_at(conn, "story", "Sample_Story", 2, "ib") is None
    assert q.pending_post_at(conn, "artwork", "Sample_Story", 2, "ws") is None
    q.add_to_queue(conn, "Piece", 0, "ws", content_type="artwork", variant_key="clean")
    assert q.pending_post_at(conn, "artwork", "Piece", 0, "ws", variant_key="clean") == ""    # as soon as possible
    assert q.pending_post_at(conn, "artwork", "Piece", 0, "ws") is None                       # another version
    q.add_to_queue(conn, "Sample_Story", 4, "ws", action="update")
    assert q.pending_post_at(conn, "story", "Sample_Story", 4, "ws") is None                  # an edit, not a post
    conn.execute("UPDATE posting_queue SET status = 'completed' WHERE chapter_index = 2")
    conn.commit()
    assert q.pending_post_at(conn, "story", "Sample_Story", 2, "ws") is None                  # done = not waiting


def test_artwork_schedule_asks_before_queuing_it_twice(client, conn, monkeypatch):
    from routes import artwork_api
    conn.close()
    monkeypatch.setattr(artwork_api.artwork_reader, "load_artwork", lambda name: {})
    monkeypatch.setattr(artwork_api, "_store_comments", lambda *a, **k: None)
    body = {"artwork_name": "Sample_Piece", "platform": "ws", "scheduled_at": "2099-01-01T00:00:00+00:00"}
    assert client.post("/api/artwork/schedule", json=body).status_code == 200
    r = client.post("/api/artwork/schedule", json=body)
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["already_scheduled"] == [{"platform": "ws", "scheduled_at": "2099-01-01 00:00:00"}]
    assert client.post("/api/artwork/schedule", json={**body, "again": True}).status_code == 200


def test_post_schedule_names_only_the_sites_already_waiting(client, conn):
    from database import posts_queries
    pid = posts_queries.create_post(conn, body="Hello")
    conn.commit()
    conn.close()
    url = f"/api/posts/{pid}/schedule"
    assert client.post(url, json={"platforms": ["bsky"], "scheduled_at": "2099-01-01T00:00:00Z"}).status_code == 200
    r = client.post(url, json={"platforms": ["bsky", "mast"], "scheduled_at": "2099-01-02T00:00:00Z"})
    assert r.status_code == 409 and [t["platform"] for t in r.json()["detail"]["already_scheduled"]] == ["bsky"]
    ok = client.post(url, json={"platforms": ["bsky", "mast"], "scheduled_at": "2099-01-02T00:00:00Z", "again": True})
    assert ok.status_code == 200 and len(ok.json()["queue_ids"]) == 2


def test_every_schedule_call_in_the_pages_goes_through_the_ask():
    api = open("frontend/js/api.js", encoding="utf-8").read()
    assert "scheduleArtwork(data) { return this._schedule(" in api
    assert "schedulePost(id, body) { return this._schedule(" in api
    assert "Schedule it again anyway?" in api and "again: true" in api
    for f in ("frontend/js/schedule_sheet.js", "frontend/js/publish_check.js"):
        src = open(f, encoding="utf-8").read()
        assert "API.scheduleStory(" in src and "/schedule'," not in src and "/schedule`, {" not in src, f
