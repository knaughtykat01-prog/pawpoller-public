"""Itaku moved its gallery API and nothing noticed for 1,955 polls (3.30.0).

Found by sweeping every platform for the identifier problem DeviantArt had.
Itaku's ids were fine. What was wrong is that `ik_submissions` was **empty**
while seven Itaku publications and seven Masterpiece members pointed into it —
so every Itaku upload had no stats, no drift detection and nothing to link
against.

The poll log explained it and hid it at the same time: both accounts, every
thirty minutes, `status='success'`, `submissions_found=0`, `error_message=None`,
finishing in under a second. Measured live against the API:

    /api/gallery_images/            -> 404
    /api/galleries/images/?owner=N  -> 200, correctly filtered to owner N
    /api/gallery_images/{id}/       -> 404
    /api/galleries/images/{id}/     -> 200

`_get_json` turns a 404 into `None`, `_paginate_content` read `None` as "no more
pages", and the poller recorded that as a clean run. **A check that cannot
fail** — it could not tell "this account has no art" from "we asked the wrong
URL". The same shape as FurAffinity's `validate_cookies`, the self-heal behind
`X or fallback()`, and the retry ceiling compared against a hardcoded zero.

Two more faults were sitting behind the first, and would have kept the fix from
working:

- **`next` moved into `links`.** The response is `{"links": {...}, "results":
  [...]}`; the client read `data["next"]`, which is now always absent, so even a
  working endpoint would have stopped after one page.
- **An unknown filter is ignored, not rejected.** `?owner__username=<name>`
  returns HTTP 200 and the site-wide feed. Storing that would file strangers'
  art under the account. `?owner=<numeric id>` is the correct filter and does
  restrict properly — verified: every owner on the page was the requested user.
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

from clients.ik.client import _PATHS, IKClient

UID = 258438


def _client(responses):
    """An IKClient whose HTTP layer replays `responses` in order."""
    c = IKClient.__new__(IKClient)
    c.target_user = "someone"
    c._user_id = UID
    c._http = None
    calls = []

    async def _get_json(url, params=None):
        calls.append((url, params))
        return responses.pop(0) if responses else None

    c._get_json = _get_json
    c.calls = calls
    return c


def _page(ids, owner=UID, nxt=None):
    return {
        "links": {"next": nxt, "previous": None},
        "results": [{"id": i, "title": "t%d" % i, "owner": owner} for i in ids],
    }


# ── the endpoints ────────────────────────────────────────────────────

def test_the_gallery_path_is_the_one_that_exists():
    """THE regression. `gallery_images` is a 404; `galleries/images` is not."""
    assert _PATHS["image"] == "galleries/images"
    assert _PATHS["post"] == "posts"


def test_the_old_path_is_gone_from_the_client_entirely():
    """List and detail both moved. Fixing one and leaving the other would look
    fixed while every stats read still 404'd.

    Matches the retired URL *path* only. `gallery_images` also survives as a
    legitimate key in the create-post body (`{"gallery_images": [ids]}`) — a
    payload field, not an endpoint — so banning the bare word would fail on it
    forever, which is how a guard stops being read.
    """
    src = (Path(__file__).resolve().parent.parent / "clients" / "ik" /
           "client.py").read_text(encoding="utf-8", errors="replace")
    code = re.sub(r"#.*", "", re.sub(r'""".*?"""', "", src, flags=re.S))
    assert not re.search(r"/gallery_images|gallery_images/", code), \
        "the retired endpoint path is still used outside comments/docstrings"


def test_the_detail_call_uses_the_same_map():
    c = _client([None])
    asyncio.run(c.get_content_detail(1355937, "image"))
    assert c.calls[0][0].endswith("/galleries/images/1355937/")


# ── an empty gallery and a dead endpoint are different things ────────

def test_a_failed_first_request_is_none_not_empty():
    """The distinction the whole bug turned on."""
    c = _client([None])
    assert asyncio.run(c._paginate_content("galleries/images", UID, "image")) is None


def test_a_genuinely_empty_gallery_is_empty_not_a_failure():
    c = _client([_page([])])
    assert asyncio.run(c._paginate_content("galleries/images", UID, "image")) == []


def test_a_failure_part_way_through_keeps_what_it_read():
    """Page one succeeded, so this is a truncated read of real data, not a dead
    endpoint — returning None would throw away rows we actually have."""
    c = _client([_page([1, 2], nxt="https://itaku.ee/api/galleries/images/?page=2"), None])
    got = asyncio.run(c._paginate_content("galleries/images", UID, "image"))
    assert [i["content_id"] for i in got] == [1, 2]


def test_discovery_raises_when_an_endpoint_is_dead():
    """A silent zero is the worst outcome, so one dead endpoint fails the poll.
    The poller turns this into `status='error'` with the message, which is what
    1,955 'successful' empty runs should have looked like."""
    c = _client([None, _page([])])
    with pytest.raises(RuntimeError, match="_PATHS"):
        asyncio.run(c.get_all_content_ids())


def test_discovery_is_happy_when_the_account_really_has_nothing():
    c = _client([_page([]), _page([])])
    assert asyncio.run(c.get_all_content_ids()) == []


# ── pagination ───────────────────────────────────────────────────────

def test_it_follows_next_inside_links():
    """`next` used to sit at the top level. Reading only there stopped every
    fetch after page one — invisible on a gallery that fits in one page."""
    c = _client([
        _page([1, 2], nxt="https://itaku.ee/api/galleries/images/?page=2"),
        _page([3, 4]),
    ])
    got = asyncio.run(c._paginate_content("galleries/images", UID, "image"))
    assert [i["content_id"] for i in got] == [1, 2, 3, 4]


def test_a_top_level_next_still_works():
    """Accepted as well as `links.next`, so the client survives Itaku moving it
    back or a cached older response shape."""
    c = _client([
        {"results": [{"id": 1, "owner": UID, "title": ""}],
         "next": "https://itaku.ee/api/galleries/images/?page=2"},
        _page([2]),
    ])
    got = asyncio.run(c._paginate_content("galleries/images", UID, "image"))
    assert [i["content_id"] for i in got] == [1, 2]


def test_the_page_two_request_carries_no_duplicate_params():
    c = _client([_page([1], nxt="https://itaku.ee/api/galleries/images/?page=2"), _page([])])
    asyncio.run(c._paginate_content("galleries/images", UID, "image"))
    assert c.calls[1][1] is None, "the cursor URL already carries the filter"


# ── never file someone else's work under this account ────────────────

def test_content_owned_by_someone_else_is_dropped():
    """This API answers an unknown filter with HTTP 200 and the site-wide feed
    rather than an error, so a renamed parameter hands back strangers' art
    looking exactly like a gallery."""
    c = _client([{
        "links": {"next": None},
        "results": [
            {"id": 1, "owner": UID, "title": "ours"},
            {"id": 2, "owner": 52927, "title": "someone else's"},
        ],
    }])
    got = asyncio.run(c._paginate_content("galleries/images", UID, "image"))
    assert [i["content_id"] for i in got] == [1]


def test_dropping_foreign_content_is_loud():
    """Silently filtering would hide the very drift that caused this."""
    import logging
    c = _client([{"links": {"next": None},
                  "results": [{"id": 2, "owner": 52927, "title": "x"}]}])
    logger = logging.getLogger("clients.ik.client")
    records = []
    h = logging.Handler()
    h.emit = records.append
    logger.addHandler(h)
    try:
        asyncio.run(c._paginate_content("galleries/images", UID, "image"))
    finally:
        logger.removeHandler(h)
    assert any("not owned by" in r.getMessage() for r in records)


def test_an_item_with_no_owner_field_is_kept():
    """Absent is not the same as wrong — a detail-light payload should not
    silently empty the gallery, which is the failure being fixed."""
    c = _client([{"links": {"next": None}, "results": [{"id": 7, "title": "x"}]}])
    got = asyncio.run(c._paginate_content("galleries/images", UID, "image"))
    assert [i["content_id"] for i in got] == [7]


def test_the_content_type_recorded_matches_what_was_asked_for():
    """It used to be inferred from the endpoint string (`== "gallery_images"`),
    which the rename silently turned into "everything is a post"."""
    c = _client([_page([1])])
    got = asyncio.run(c._paginate_content("galleries/images", UID, "image"))
    assert got[0]["content_type"] == "image"
    c = _client([_page([2])])
    got = asyncio.run(c._paginate_content("posts", UID, "post"))
    assert got[0]["content_type"] == "post"
