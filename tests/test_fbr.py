"""Furbooru (Philomena) — client parsing + poll cycle (network faked).

Locks: the Philomena image → normalised (score/up/down/faves/comments) parse,
rating-from-tags, paginated discovery, and the full poll cycle storing data.
"""
import asyncio

import httpx
import pytest

import config
from database.db import get_connection
from database import fbr_queries
from clients.fbr.client import FurbooruClient
import polling.fbr_poller as fp


def _img(iid, score=10, up=12, down=2, faves=5, comments=1, tags=None):
    return {"id": iid, "score": score, "upvotes": up, "downvotes": down,
            "faves": faves, "comment_count": comments,
            "tags": tags or ["safe", "pony"], "format": "png",
            "view_url": f"http://cdn/{iid}.png",
            "representations": {"thumb": f"http://cdn/{iid}_thumb.png",
                                "full": f"http://cdn/{iid}.png"},
            "created_at": "2024-01-01T00:00:00", "description": ""}


class _FakeClient:
    """Serves the Philomena search API from a fixed set of images."""
    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def aclose(self):
        pass

    async def get(self, url, params=None):
        page = int((params or {}).get("page", 1))
        per = int((params or {}).get("per_page", 50))
        if per == 1:                       # validate_session probe
            return _Resp({"images": [_img(1)]})
        images = [_img(1, tags=["explicit"]), _img(2, tags=["questionable"])] if page == 1 else []
        return _Resp({"images": images, "total": 2})


class _Resp:
    def __init__(self, payload, status=200):
        self._p, self.status_code = payload, status

    def json(self):
        return self._p


@pytest.fixture
def _fake_net(monkeypatch):
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)


def _run(c):
    return asyncio.run(c)


def test_parse_image_maps_to_score_model():
    c = FurbooruClient(username="kit")
    d = c._parse_image(_img(7, score=50, up=55, down=5, faves=20, comments=3, tags=["explicit", "wolf"]))
    assert d["post_uri"] == "7"
    assert d["score"] == 50 and d["up_score"] == 55 and d["down_score"] == 5
    assert d["favorites_count"] == 20 and d["comments_count"] == 3
    assert d["rating"] == "adult"                     # 'explicit' tag → adult
    assert d["keywords"] == ["explicit", "wolf"]
    assert "furbooru.org/images/7" in d["link"]
    assert d["thumbnail_url"].endswith("_thumb.png")


def test_rating_from_tags():
    c = FurbooruClient(username="kit")
    assert c._parse_image(_img(1, tags=["questionable"]))["rating"] == "mature"
    assert c._parse_image(_img(1, tags=["safe"]))["rating"] == "general"


def test_discovery_and_validate(_fake_net):
    c = FurbooruClient(username="kit")
    assert _run(c.validate_session()) == "kit"
    items = _run(c.get_all_post_uris())
    assert [it["post_uri"] for it in items] == ["1", "2"]


class _FakeFbrClient:
    def __init__(self, **kw):
        self.username = kw.get("username", "")
    def update_credentials(self, u, k):
        self.username = u
    async def close(self):
        pass
    async def validate_session(self):
        return "kit"
    async def get_all_post_uris(self):
        return [{"post_uri": "1", "raw": {}}, {"post_uri": "2", "raw": {}}]
    async def get_post_details_batch(self, items):
        fc = FurbooruClient(username="kit")
        return [fc._parse_image(_img(1, score=50, faves=20)),
                fc._parse_image(_img(2, score=5, faves=1))]


def test_poll_cycle_stores_data(monkeypatch):
    config.save_settings({"fbr_username": "kit"})
    monkeypatch.setattr(fp, "FurbooruClient", _FakeFbrClient)
    fp._fbr_client = None
    fp._fbr_first_poll_done.clear()
    stats = _run(fp.run_fbr_poll_cycle())
    assert stats["submissions_found"] == 2 and stats["snapshots_inserted"] == 2
    conn = get_connection()
    try:
        subs = fbr_queries.get_all_fbr_submissions(conn)
        assert {s["submission_id"] for s in subs} == {"1", "2"}
        assert fbr_queries.get_fbr_submission(conn, "1")["score"] == 50
        assert fbr_queries.get_fbr_last_poll(conn)["status"] == "success"
    finally:
        conn.close()
