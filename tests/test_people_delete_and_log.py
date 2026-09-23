"""Two things a tester hit on 4.32.2 (backlog PEOPLEDEL, POSTLOG).

PEOPLEDEL: the People page could add, rename and drop a single handle, but never remove a
person — a typo'd or duplicated entry stayed forever. Deleting one must not touch the
archive: a credit is written inline on each `masterpiece.json`, so the pieces keep the
name and simply stop resolving it to handles.

POSTLOG: `/api/posting/log` inherited `get_posting_log`'s `content_type="story"` default
while the page calls itself "Posting History", so anyone posting artwork saw "No posting
activity yet". `laurels.js` had been asking for everything and being ignored.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from database import artist_queries as aq
from database import posting_queries
from database.db import get_connection


@pytest.fixture()
def people_client():
    from routes.artists_api import artists_router
    app = FastAPI()
    app.include_router(artists_router)
    return TestClient(app)


class TestRemovingAPerson:
    def test_a_person_with_no_credits_goes_quietly(self, people_client):
        conn = get_connection()
        aq.upsert_artist(conn, "Inkwolf", handles={"fa": "inkwolf"})
        conn.commit()
        key = aq.artist_key("Inkwolf")
        conn.close()

        r = people_client.delete(f"/api/artists/{key}")
        assert r.status_code == 200, r.text
        assert r.json()["name"] == "Inkwolf"

        conn = get_connection()
        assert aq.get_artist(conn, key) is None
        assert conn.execute("SELECT COUNT(*) FROM artist_handles WHERE artist_key = ?",
                            (key,)).fetchone()[0] == 0
        conn.close()

    def test_deleting_an_unknown_person_is_a_404(self, people_client):
        assert people_client.delete("/api/artists/nobody-at-all").status_code == 404

    def test_the_handles_go_with_them(self):
        conn = get_connection()
        aq.upsert_artist(conn, "Penwright", handles={"fa": "penwright", "bsky": "pen.bsky.social"})
        conn.commit()
        key = aq.artist_key("Penwright")
        removed = aq.delete_artist(conn, key)
        conn.commit()
        assert removed["name"] == "Penwright"
        assert aq.get_artist(conn, key) is None
        assert conn.execute("SELECT COUNT(*) FROM artist_handles WHERE artist_key = ?",
                            (key,)).fetchone()[0] == 0
        conn.close()

    def test_deleting_twice_raises(self):
        conn = get_connection()
        aq.upsert_artist(conn, "Gone Already")
        conn.commit()
        key = aq.artist_key("Gone Already")
        aq.delete_artist(conn, key)
        conn.commit()
        with pytest.raises(KeyError):
            aq.delete_artist(conn, key)
        conn.close()


class TestThePostingHistory:
    @pytest.fixture()
    def log_client(self):
        from routes.posting_api import posting_router
        app = FastAPI()
        app.include_router(posting_router)
        return TestClient(app)

    @staticmethod
    def _seed():
        conn = get_connection()
        posting_queries.log_posting_action(
            conn, story_name="Sample Story", chapter_index=0, platform="fa",
            action="post", status="success", content_type="story")
        posting_queries.log_posting_action(
            conn, story_name="Sample Piece", chapter_index=0, platform="bsky",
            action="post", status="success", content_type="artwork")
        conn.commit()
        conn.close()

    def test_the_history_shows_artwork_too(self, log_client):
        self._seed()
        log = log_client.get("/api/posting/log?limit=50").json()["log"]
        kinds = {e["content_type"] for e in log}
        assert "artwork" in kinds and "story" in kinds, kinds

    def test_a_caller_can_still_narrow_it(self, log_client):
        self._seed()
        log = log_client.get("/api/posting/log?limit=50&content_type=artwork").json()["log"]
        assert log and {e["content_type"] for e in log} == {"artwork"}

    @pytest.mark.parametrize("value", ["null", "none", "all", ""])
    def test_a_query_string_cannot_carry_a_null(self, log_client, value):
        """laurels.js sends content_type: null — in a URL that is the word, not nothing."""
        self._seed()
        log = log_client.get(f"/api/posting/log?limit=50&content_type={value}").json()["log"]
        assert {e["content_type"] for e in log} >= {"artwork", "story"}
