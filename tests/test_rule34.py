"""Rule34.xxx tracking (spec 028 US6): the client's parsing, auth refusal, paging and the connect checks."""
import asyncio

import pytest

from clients.r34 import client as r34


def _run(coro):
    return asyncio.run(coro)


def test_parse_maps_score_comments_and_no_favourites():
    c = r34.Rule34Client("Inkwolf", "k", "123")
    d = c._parse({"id": 77, "tags": "inkwolf sample_tag", "file_url": "https://x/a.webm", "rating": "explicit",
                  "score": "12", "comment_count": 3, "creator_id": 123, "owner": "Inkwolf"})
    assert d["post_uri"] == "77" and d["title"] == "#77"
    assert d["score"] == 12 and d["comments_count"] == 3 and d["favorites_count"] == 0
    assert d["content_type"] == "video" and d["rating"] == "adult"
    assert d["keywords"] == ["inkwolf", "sample_tag"]
    assert d["uploader_id"] == "123" and d["uploader_name"] == "Inkwolf"
    assert d["link"].endswith("page=post&s=view&id=77")


class _Resp:
    def __init__(self, data):
        self._data = data
        self.text = "x" if data is not None else ""

    def json(self):
        return self._data


class _Http:
    def __init__(self, data):
        self.data = data

    async def get(self, url, params=None):
        return _Resp(self.data)


def test_missing_auth_string_raises_and_validate_returns_none():
    c = r34.Rule34Client("Inkwolf", "bad", "1")
    c._client = _Http("Missing authentication. Go to api.rule34.xxx for more information")
    with pytest.raises(r34.Rule34AuthError):
        _run(c._posts("", 0))
    assert _run(c.validate_session()) is None


def test_validate_needs_key_and_id():
    assert _run(r34.Rule34Client("Inkwolf", "", "1").validate_session()) is None
    assert _run(r34.Rule34Client("Inkwolf", "k", "").validate_session()) is None


def test_bad_deep_page_restarts_below_lowest_id(monkeypatch):
    monkeypatch.setattr(r34, "PER_PAGE", 2)
    monkeypatch.setattr(r34, "REQUEST_DELAY", 0)
    calls = []

    async def fake(self, tags, pid):
        calls.append((tags, pid))
        if tags == "t" and pid == 0:
            return [{"id": 10}, {"id": 9}]
        if tags == "t" and pid == 1:
            return None                       # unreadable page
        if tags == "t id:<9":
            return [{"id": 8}]
        return []

    monkeypatch.setattr(r34.Rule34Client, "_posts", fake)
    out = _run(r34.Rule34Client("Inkwolf", "k", "1").search("t"))
    assert [p["id"] for p in out] == [10, 9, 8]
    assert ("t id:<9", 0) in calls


def test_uploads_search_comes_first_and_merges_by_id(monkeypatch):
    monkeypatch.setattr(r34, "REQUEST_DELAY", 0)
    asked = []

    async def fake(self, tags, known=None, seen=None):
        asked.append(tags)
        rows = {"user:Inkwolf": [{"id": 1}], "inkwolf": [{"id": 2}]}.get(tags, [])
        return [p for p in rows if str(p["id"]) not in seen and not seen.add(str(p["id"]))]

    monkeypatch.setattr(r34.Rule34Client, "search", fake)
    items = _run(r34.Rule34Client("Inkwolf", "k", "1").get_all_post_uris(["inkwolf"]))
    assert asked == ["user:Inkwolf", "inkwolf"]
    assert [i["post_uri"] for i in items] == ["1", "2"]


def test_connect_requires_all_three_and_a_numeric_id():
    from fastapi import HTTPException
    from routes.r34_api import r34_connect
    with pytest.raises(HTTPException) as e:
        _run(r34_connect({"username": "Inkwolf", "api_key": "k"}))
    assert e.value.status_code == 400
    with pytest.raises(HTTPException) as e:
        _run(r34_connect({"username": "Inkwolf", "api_key": "k", "user_id": "abc"}))
    assert e.value.status_code == 400
