"""BUGHUNT step 3: the story publish / schedule routes (routes/editor_api.py).

- Publishing a site's draft flipped it as the platform's default account, not the account the chapter
  was posted as, so a second account's draft was looked for on the wrong account.
- A schedule refused as a repeat (SCHEDTWICE's 409) had already replaced the chapter's paired comment.
- A chapter number past the end was accepted: build_package quietly falls back to the whole story, so
  "post chapter 99" or a schedule for it went ahead as something else.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import config
from database import posting_queries
from database.db import get_connection
from routes import editor_api


@pytest.fixture
def api(story_archive):
    import dashboard
    config.save_settings({"posting_story_archive_path": str(story_archive)})
    return TestClient(dashboard.app, raise_server_exceptions=False, client=("127.0.0.1", 50000))


class _Poster:
    requires_mode = "any"

    def __init__(self, account_id=None):
        self.account_id = account_id

    def validate(self, package):
        return []

    async def publish_draft(self, external_id):
        return SimpleNamespace(success=True, external_id=external_id, external_url="", error=None)


@pytest.fixture
def posters(monkeypatch):
    from posting import manager
    seen: list = []

    def get(platform, account_id=None):
        seen.append((platform, account_id))
        return _Poster(account_id)

    monkeypatch.setattr(manager, "_get_poster", get)
    return seen


def test_publishing_a_draft_uses_the_account_it_was_posted_as(api, posters):
    conn = get_connection()
    try:
        posting_queries.upsert_publication(conn, "Test_Story", 1, "sf", account_id=7, external_id="555")
    finally:
        conn.close()
    r = api.post("/api/editor/stories/Test_Story/publish",
                 json={"platform": "sf", "chapter": 1, "action": "publish_draft", "confirm_live": True})
    assert r.status_code == 200, r.text
    assert posters[-1] == ("sf", 7)


def test_a_refused_repeat_schedule_keeps_the_stored_comment(api, posters, monkeypatch):
    stored: list = []
    monkeypatch.setattr(editor_api, "_store_comments", lambda *a: stored.append(a))
    body = {"platform": "sf", "chapter": 1, "scheduled_at": "2099-01-01T10:00:00Z", "comment": "first"}
    assert api.post("/api/editor/stories/Test_Story/schedule", json=body).status_code == 200
    r = api.post("/api/editor/stories/Test_Story/schedule", json={**body, "comment": "second"})
    assert r.status_code == 409, r.text
    assert len(stored) == 1, "the refused schedule must not touch the comment"


@pytest.mark.parametrize("chapter", [3, 99, -1])
def test_a_chapter_past_the_end_is_refused(api, posters, chapter):
    for path, body in (
        ("publish", {"action": "dry_run"}),
        ("publish", {"action": "post", "confirm_live": True}),
        ("schedule", {"scheduled_at": "2099-01-01T10:00:00Z"}),
    ):
        r = api.post(f"/api/editor/stories/Test_Story/{path}", json={"platform": "sf", "chapter": chapter, **body})
        assert r.status_code == 400 and "Chapter" in r.text, (path, body, r.status_code, r.text[:200])
    conn = get_connection()
    try:
        assert posting_queries.get_queue(conn, story_name="Test_Story") == []
    finally:
        conn.close()


def test_the_whole_story_and_real_chapters_still_schedule(api, posters):
    for ch in (0, 1, 2):
        r = api.post("/api/editor/stories/Test_Story/schedule",
                     json={"platform": "sf", "chapter": ch, "scheduled_at": "2099-01-01T10:00:00Z"})
        assert r.status_code == 200, (ch, r.text)


def test_cancel_refuses_another_storys_row(api, story_archive, posters):
    conn = get_connection()
    try:
        other = posting_queries.add_to_queue(conn, "Other_Story", 1, "sf", action="post",
                                             scheduled_at="2099-01-01 10:00:00")
    finally:
        conn.close()
    assert api.delete(f"/api/editor/stories/Test_Story/scheduled/{other}").status_code == 404
    conn = get_connection()
    try:
        assert [i["queue_id"] for i in posting_queries.get_queue(conn, story_name="Other_Story")] == [other]
    finally:
        conn.close()


def test_a_refused_repeat_artwork_schedule_keeps_the_stored_comment(api, monkeypatch):
    from routes import artwork_api
    stored: list = []
    monkeypatch.setattr(artwork_api.artwork_reader, "load_artwork", lambda name: {})
    monkeypatch.setattr(artwork_api, "_store_comments", lambda *a, **k: stored.append(a))
    body = {"artwork_name": "Sample_Piece", "platform": "ws", "scheduled_at": "2099-01-01T00:00:00+00:00",
            "comments": {"ws": "first"}}
    assert api.post("/api/artwork/schedule", json=body).status_code == 200
    r = api.post("/api/artwork/schedule", json={**body, "comments": {"ws": "second"}})
    assert r.status_code == 409, r.text
    assert len(stored) == 1, "the refused schedule must not touch the comment"
