"""SoFurry submission-detail parsing (rewritten for 3.4.0).

get_submission_detail reads two things from two places, and the split is not
arbitrary — SoFurry's official API returns no statistics at all, so:

  * views / likes / title / thumbnail  ← GET sofurry.com/api/submission/{id} (JSON)
  * comment count                      ← GET sofurry.com/s/{id}.data (turbo-stream)

Both are login-free. No network here — the web client's .get is faked.

**Why this file changed.** Until 3.4.0 stats were regex-scraped out of the
turbo-stream payload with `"key",(\\d+)`, which assumes a value always sits
immediately after its key. That payload is devalue-encoded with a de-duplicated
value table: a small integer already present in the table is not re-emitted, so
the key is followed by the *next key* and the parse silently returned 0. Like
counts are exactly those small integers, so SF favourites were under-counted in
production.

The previous version of this test could never have caught it: its fixture was
hand-written as `"views",858,"likes",42` — every value fresh and inline, the one
shape the old regex handled. `test_deduplicated_like_count_would_have_broken_the_old_parser`
below pins a realistic payload instead.
"""
import asyncio

import pytest


class _Resp:
    def __init__(self, *, text="", json_body=None, status=200):
        self.text = text
        self._json = json_body
        self.status_code = status

    def json(self):
        if self._json is None:
            raise ValueError("not json")
        return self._json


def _client(json_body=None, data_payload="", json_status=200):
    """A client whose web surface answers both reads without touching the network."""
    from clients.sf.client import SoFurryClient
    c = SoFurryClient()

    async def fake_get(url, **kw):
        if "/api/submission/" in url:
            return _Resp(json_body=json_body, status=json_status)
        return _Resp(text=data_payload)

    c._web.get = fake_get
    return c


SUBMISSION_JSON = {
    "submission": {
        "id": "1YAApVD1",
        "title": "A Sample Piece",
        "description": "a ref",
        "publishedAt": "2026-02-19",
        "category": "artwork",
        "rating": 20,
        "views": 858,
        "likes": 42,
        "tags": ["dragon", "ref sheet"],
        "thumbUrl": "https://cdn.sofurryfiles.com/submissions/thumbnails/6b/e3/6be33fb0",
    }
}

COMMENTS_PAYLOAD = '["perPage",20,"total",12,"hasMore",false]'


def test_extracts_stats_and_metadata():
    d = asyncio.run(_client(SUBMISSION_JSON, COMMENTS_PAYLOAD).get_submission_detail("1YAApVD1"))
    assert d["title"] == "A Sample Piece"
    assert d["views"] == 858
    assert d["favorites_count"] == 42
    assert d["comments_count"] == 12
    assert d["thumbnail_url"] == \
        "https://cdn.sofurryfiles.com/submissions/thumbnails/6b/e3/6be33fb0"
    assert d["keywords"] == ["dragon", "ref sheet"]
    # rating was never populated before 3.4.0; it now carries the human label.
    assert d["rating"] == "Adult"


def test_deduplicated_like_count_would_have_broken_the_old_parser():
    """The production failure mode, pinned.

    This is a realistic turbo-stream fragment: `likes` is followed by the NEXT
    KEY, not by its value, because the like count was de-duplicated into the
    shared value table. The old `"likes",(\\d+)` regex read 0 here. Reading JSON
    instead makes the payload's encoding irrelevant, so the count is correct.
    """
    payload = ('["views",1692,"likes","allowComments",true,"allowDownloads",'
               '"privacy",3,"perPage",20,"total",2,"hasMore",false]')
    body = {"submission": {"title": "Second Sample", "views": 1692, "likes": 20}}
    d = asyncio.run(_client(body, payload).get_submission_detail("ebQ4Jkd1"))
    assert d["favorites_count"] == 20, "likes must not depend on turbo-stream ordering"
    assert d["views"] == 1692
    assert d["comments_count"] == 2


def test_text_work_has_no_thumbnail():
    body = {"submission": {"title": "My Story", "views": 10, "likes": 3, "thumbUrl": None}}
    d = asyncio.run(_client(body, '["total",0,"hasMore",false]').get_submission_detail("abc"))
    assert d["thumbnail_url"] == ""
    assert d["title"] == "My Story"
    assert d["comments_count"] == 0


def test_unknown_comment_count_is_none_not_zero():
    """A failed comment fetch must be distinguishable from "no comments".

    Writing 0 would read as every comment having been deleted, and the next
    successful poll would then re-report them all as new. The poller carries the
    previous value forward when this is None.
    """
    d = asyncio.run(_client(SUBMISSION_JSON, "no total marker here").get_submission_detail("x"))
    assert d["comments_count"] is None
    assert d["views"] == 858, "a comment-fetch failure must not disturb the other stats"


def test_failed_metadata_fetch_leaves_stats_at_zero():
    """The poller's zero-view guard depends on this staying 0 rather than raising."""
    d = asyncio.run(_client(None, COMMENTS_PAYLOAD, json_status=404).get_submission_detail("gone"))
    assert d["views"] == 0
    assert d["favorites_count"] == 0
    assert d["title"] == ""
