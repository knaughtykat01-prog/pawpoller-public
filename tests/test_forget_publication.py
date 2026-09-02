"""Forgetting a post that was deleted upstream (3.17.0).

**PawPoller cannot see a deletion made on the site.** Nothing polls for "is this
submission still there", and no platform volunteers it. So deleting an upload on
Bluesky/FA/wherever leaves the local records untouched, and the Masterpiece page
keeps that platform dimmed and disabled ("Already on this site") — which reads,
correctly, as being locked out of re-posting a piece that is no longer anywhere.

The real case: a piece posted to the wrong Bluesky account, deleted on Bluesky,
and then not re-postable to the right one.

Two records drive that dimming (`masterpieces.js` `_wireDetailPublish`) and both
have to go:

  * the `publications` row — "posted here, at this account";
  * the `masterpiece_members` row — the pooled-stats link.

`DELETE /{name}/members` only ever removed the second. The story side has had
the first since the publish-check panel, but that route resolves against the
STORY archive, so it could never serve an artwork.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from database import masterpiece_queries as mq
from database.db import get_connection
from posting import artwork_reader


@pytest.fixture()
def client():
    from dashboard import app
    return TestClient(app)


@pytest.fixture()
def work(tmp_path, monkeypatch):
    root = tmp_path / "artwork"
    folder = root / "OldTimeySuit"
    folder.mkdir(parents=True)
    (folder / "image.jpg").write_bytes(b"\xff\xd8\xff\xd9")
    (folder / "masterpiece.json").write_text(json.dumps({
        "title": "Old Timey Suit", "rating": "general", "image": "image.jpg",
        "tags": {"core": ["a", "b"]},
    }), encoding="utf-8")
    monkeypatch.setattr(artwork_reader, "get_artwork_archive_path", lambda: root)
    return "OldTimeySuit"


def _post_it(name, platform="bsky", ext="3mtjeg3r7ed2f", account=11,
             member=True):
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO publications (content_type, story_name, chapter_index, "
            "platform, account_id, external_id, external_url, status) "
            "VALUES ('artwork', ?, 0, ?, ?, ?, ?, 'posted')",
            (name, platform, account, ext, f"https://bsky.app/x/{ext}"))
        if member:
            mq.add_member(conn, name, platform, ext, account_id=account)
        conn.commit()
    finally:
        conn.close()


def _state(name, platform="bsky"):
    conn = get_connection()
    try:
        pub = conn.execute(
            "SELECT 1 FROM publications WHERE story_name = ? AND platform = ? "
            "AND content_type = 'artwork'", (name, platform)).fetchone()
        mem = conn.execute(
            "SELECT 1 FROM masterpiece_members WHERE masterpiece_name = ? "
            "AND platform = ?", (name, platform)).fetchone()
        return bool(pub), bool(mem)
    finally:
        conn.close()


# ── the unlock ───────────────────────────────────────────────────

def test_forgetting_clears_both_records(client, work):
    """Either record left behind keeps the checkbox disabled, so clearing one
    is not a fix. `DELETE /{name}/members` did exactly that."""
    _post_it(work)
    r = client.delete(
        f"/api/masterpieces/{work}/publication",
        params={"platform": "bsky", "confirm_platform": "bsky"})
    assert r.status_code == 200, r.text
    assert _state(work) == (False, False)


def test_it_reports_what_it_removed(client, work):
    _post_it(work)
    body = client.delete(
        f"/api/masterpieces/{work}/publication",
        params={"platform": "bsky", "confirm_platform": "bsky"}).json()
    assert body["publication_removed"] is True
    assert body["members_removed"] == ["3mtjeg3r7ed2f"]
    assert body["account_id"] == 11


def test_the_forgotten_link_comes_back_so_the_action_can_be_undone(client, work):
    """This is the reversibility story: the response hands back the URL, and
    `POST /{name}/link-url` takes it straight back. Nothing is unrecoverable."""
    _post_it(work)
    body = client.delete(
        f"/api/masterpieces/{work}/publication",
        params={"platform": "bsky", "confirm_platform": "bsky"}).json()
    assert body["external_id"] == "3mtjeg3r7ed2f"
    assert body["external_url"]


def test_only_the_named_platform_is_touched(client, work):
    _post_it(work, "bsky", "aaa", 11)
    _post_it(work, "ib", "222", 22)
    client.delete(f"/api/masterpieces/{work}/publication",
                  params={"platform": "bsky", "confirm_platform": "bsky"})
    assert _state(work, "bsky") == (False, False)
    assert _state(work, "ib") == (True, True)


def test_a_publication_with_no_member_is_still_forgettable(client, work):
    """The real Bluesky case had a publications row and NO member row — the
    post was recorded but never linked as a stats member."""
    _post_it(work, member=False)
    r = client.delete(f"/api/masterpieces/{work}/publication",
                      params={"platform": "bsky", "confirm_platform": "bsky"})
    assert r.status_code == 200
    assert _state(work) == (False, False)


def test_keep_member_drops_only_the_publication(client, work):
    """For a post that still EXISTS but was recorded under the wrong account:
    the stats link is real and worth keeping."""
    _post_it(work)
    client.delete(f"/api/masterpieces/{work}/publication",
                  params={"platform": "bsky", "confirm_platform": "bsky",
                          "keep_member": "true"})
    assert _state(work) == (False, True)


# ── the guard ────────────────────────────────────────────────────

def test_the_confirm_gate_matches_the_story_route(client, work):
    _post_it(work)
    r = client.delete(f"/api/masterpieces/{work}/publication",
                      params={"platform": "bsky", "confirm_platform": "ib"})
    assert r.status_code == 400
    assert _state(work) == (True, True), "nothing may be removed on a failed gate"


def test_a_missing_confirm_removes_nothing(client, work):
    _post_it(work)
    assert client.delete(f"/api/masterpieces/{work}/publication",
                         params={"platform": "bsky"}).status_code == 400
    assert _state(work) == (True, True)


def test_forgetting_something_never_recorded_says_so(client, work):
    r = client.delete(f"/api/masterpieces/{work}/publication",
                      params={"platform": "bsky", "confirm_platform": "bsky"})
    assert r.status_code == 404
    assert "already clear" in r.json()["detail"]


# ── the URL builder it leans on ──────────────────────────────────

def test_build_url_is_the_inverse_of_the_parser():
    from posting.submission_urls import build_url, parse_submission_url
    for platform, sid in [("fa", "37056160"), ("e621", "1955656"),
                          ("ib", "123456"), ("ws", "55555")]:
        assert parse_submission_url(build_url(platform, sid)) == (platform, sid)


def test_build_url_refuses_rather_than_returning_a_dead_link():
    """bsky's template has a `_` where a user handle belongs; formatting it
    yields a URL that does not resolve. Empty is the honest answer."""
    from posting.submission_urls import build_url
    assert build_url("bsky", "abc") == ""
    assert build_url("nonesuch", "1") == ""
    assert build_url("fa", "") == ""
