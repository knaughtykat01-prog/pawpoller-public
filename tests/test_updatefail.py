"""UPDATEFAIL: an update never re-adds chapters it can't see, and a failed chapter step fails the update.

Stand-in clients and placeholder titles only: nothing here reaches a site.
"""
import ast
import asyncio
import inspect
from types import SimpleNamespace as NS

import pytest

from clients.ao3.client import AO3Client
from clients.sf.client import SoFurryClient, SoFurryError
from clients.sqw.client import SquidgeWorldClient
from database import posting_queries as pq
from database.db import get_connection
from posting import manager, scheduler, story_reader
from posting.platforms.ao3 import AO3Poster
from posting.platforms.base import PostResult, StoryUploadPackage
from posting.platforms.sofurry import SoFurryPoster
from posting.platforms.squidgeworld import SquidgeWorldPoster

STORY = "Updatefail_Sample"
WORK = "900"
SITES = ["ao3", "sqw", "sf"]
POSTERS = {"ao3": AO3Poster, "sqw": SquidgeWorldPoster, "sf": SoFurryPoster}


class Stand:
    """Stands in for all three site clients and records every write."""

    def __init__(self, count=3):
        self.ids = [str(100 + i) for i in range(count)]
        self.list_error = None
        self.fail_on = set()
        self.writes = []
        self.go_live_failed = []

    def _write(self, kind, key):
        if key in self.fail_on:
            raise RuntimeError("the site said no")
        self.writes.append((kind, key))

    def edits(self):
        return [key for kind, key in self.writes if kind == "edit"]

    async def edit_work(self, *a, **k):
        return {}

    async def is_work_in_drafts(self, work):
        return False

    async def is_work_published(self, work):
        return True

    async def get_chapter_ids(self, work):
        if self.list_error:
            raise self.list_error
        return [{"chapter_id": c, "index": i, "title": f"old {i}"} for i, c in enumerate(self.ids, 1)]

    async def edit_chapter(self, work, cid, **k):
        self._write("edit", cid)

    async def create_chapter(self, work, **k):
        self._write("create", str(k["position"]))

    async def publish_all_draft_chapters(self, work):
        return {"published": 0, "already_posted": 3, "failed": self.go_live_failed, "total": 3}

    async def edit_submission(self, *a, **k):
        return {"url": "https://example.test/s/900"}

    async def get_content_ids(self, sid):
        if self.list_error:
            raise self.list_error
        return list(self.ids)

    async def update_content(self, sid, cid, **k):
        self._write("edit", cid)

    async def upload_content(self, sid, path):
        self._write("upload", "new")
        return "500"

    async def set_content_title(self, sid, cid, title):
        self._write("title", cid)


async def _none(*a, **k):
    return None


@pytest.fixture()
def story(monkeypatch, tmp_path):
    s = NS(name=STORY, path=tmp_path, total_chapters=3, rating="explicit", fandom="", category="", categories=None,
           warnings=None, characters=[], relationships=[], description="",
           chapters=[NS(index=i, title=t, filename="") for i, t in enumerate(("One", "Two", "Three"), 1)])
    monkeypatch.setattr(story_reader, "load_story", lambda name: s)
    return s


@pytest.fixture()
def poster_for(monkeypatch, tmp_path):
    body = tmp_path / "chapter.html"
    body.write_text("<p>body</p>", encoding="utf-8")

    def build(site, client, missing=()):
        cls = POSTERS[site]
        poster = cls()

        async def ensure():
            return client

        monkeypatch.setattr(poster, "_ensure_client", ensure)
        if site == "sf":
            monkeypatch.setattr(poster, "_read_sf_chapter_content",
                                lambda story, i: None if i in missing else str(body))
        else:
            monkeypatch.setattr(poster, "_ensure_work_skin", _none)
            monkeypatch.setattr(poster, "_build_freeform_tags", lambda *a, **k: [])
            monkeypatch.setattr(poster, "_trim_freeform_tags", lambda *a, **k: [])
            monkeypatch.setattr(poster, "_read_chapter_content",
                                lambda story, i: None if i in missing else "<p>body</p>")
        return poster

    return build


def _package(site, **extra):
    return StoryUploadPackage(story_name=STORY, chapter_index=0, chapter_title="", platform=site, title="Sample Story",
                              description="", rating="explicit", file_type="html",
                              extra={"allow_state_change": True, **extra})


def _edit(poster, site, **extra):
    return asyncio.run(poster.edit(WORK, _package(site, **extra)))


@pytest.mark.parametrize("site", SITES)
def test_a_clean_update_sends_every_chapter(story, poster_for, site):
    client = Stand()
    r = _edit(poster_for(site, client), site)
    assert r.success, r.error
    assert client.edits() == client.ids


@pytest.mark.parametrize("site", SITES)
def test_unreadable_list_stops_and_writes_nothing(story, poster_for, site):
    client = Stand()
    client.list_error = RuntimeError("Couldn't read the list")
    r = _edit(poster_for(site, client), site)
    assert not r.success
    assert client.writes == []


@pytest.mark.parametrize("site", SITES)
def test_failed_chapter_stops_and_is_named(story, poster_for, site):
    client = Stand()
    client.fail_on.add("101")
    r = _edit(poster_for(site, client), site)
    assert not r.success and "Chapter 2" in r.error
    assert "102" not in client.edits()


@pytest.mark.parametrize("site", SITES)
def test_missing_file_fails_instead_of_skipping(story, poster_for, site):
    client = Stand()
    r = _edit(poster_for(site, client, missing={2}), site)
    assert not r.success and "Chapter 2" in r.error
    assert "102" not in client.edits()


@pytest.mark.parametrize("site", SITES)
def test_a_failed_chapter_is_not_read_as_a_deleted_work(story, poster_for, site):
    client = Stand()
    client.fail_on.add("100")
    assert not manager._looks_like_deletion(_edit(poster_for(site, client), site).error)


def test_ao3_still_adds_a_chapter_the_site_really_lacks(story, poster_for):
    client = Stand(count=2)
    r = _edit(poster_for("ao3", client), "ao3")
    assert r.success, r.error
    assert client.writes == [("edit", "100"), ("edit", "101"), ("create", "3")]


def test_ao3_failed_new_chapter_is_named(story, poster_for):
    client = Stand(count=2)
    client.fail_on.add("3")
    r = _edit(poster_for("ao3", client), "ao3")
    assert not r.success and "Chapter 3" in r.error


@pytest.mark.parametrize("site", ["ao3", "sqw"])
def test_titles_only_update_reports_a_failed_title(story, poster_for, site):
    client = Stand()
    client.fail_on.add("101")
    r = _edit(poster_for(site, client), site, skip_content_refresh=True)
    assert not r.success and "Chapter 2" in r.error


@pytest.mark.parametrize("site", ["ao3", "sqw"])
def test_titles_only_update_stops_on_an_unreadable_list(story, poster_for, site):
    client = Stand()
    client.list_error = RuntimeError("Couldn't read the list")
    r = _edit(poster_for(site, client), site, skip_content_refresh=True)
    assert not r.success and client.writes == []


@pytest.mark.parametrize("extra", [{}, {"skip_content_refresh": True}])
def test_ao3_chapter_left_as_a_draft_fails_the_update(story, poster_for, extra):
    client = Stand()
    client.go_live_failed = [("101", "the site said no")]
    r = _edit(poster_for("ao3", client), "ao3", draft=False, **extra)
    assert not r.success and "didn't go live" in r.error


def test_ao3_single_chapter_failure_is_reported(story, poster_for, monkeypatch):
    story.total_chapters, story.chapters = 1, []
    client = Stand(count=1)
    client.fail_on.add("100")
    poster = poster_for("ao3", client)
    monkeypatch.setattr(poster, "_read_full_story_html", lambda s: "<p>whole</p>")
    r = _edit(poster, "ao3")
    assert not r.success and "Chapter 1" in r.error


def test_ao3_single_chapter_with_no_file_fails(story, poster_for, monkeypatch):
    story.total_chapters, story.chapters = 1, []
    client = Stand(count=1)
    poster = poster_for("ao3", client)
    monkeypatch.setattr(poster, "_read_full_story_html", lambda s: None)
    r = _edit(poster, "ao3")
    assert not r.success and client.writes == []


def test_ao3_single_chapter_uses_the_same_fallback_as_a_post(story, poster_for, monkeypatch, tmp_path):
    story.total_chapters, story.chapters = 1, []
    client = Stand(count=1)
    poster = poster_for("ao3", client)
    monkeypatch.setattr(poster, "_read_full_story_html", lambda s: None)
    file = tmp_path / "whole.html"
    file.write_text("<p>whole</p>", encoding="utf-8")
    package = _package("ao3")
    package.file_path = str(file)
    r = asyncio.run(poster.edit(WORK, package))
    assert r.success, r.error
    assert client.edits() == ["100"]


def test_sqw_chapter_only_on_the_site_is_left_alone(story, poster_for):
    client = Stand(count=4)
    r = _edit(poster_for("sqw", client), "sqw")
    assert r.success, r.error
    assert client.edits() == client.ids[:3]


@pytest.mark.parametrize("chapters", [[NS(index=1, title="One", filename="")], []])
def test_sqw_single_chapter_uses_the_same_fallback_as_a_post(story, poster_for, tmp_path, chapters):
    story.total_chapters, story.chapters = 1, chapters
    client = Stand(count=1)
    file = tmp_path / "whole.html"
    file.write_text("<p>whole</p>", encoding="utf-8")
    package = _package("sqw")
    package.file_path = str(file)
    r = asyncio.run(poster_for("sqw", client, missing={1}).edit(WORK, package))
    assert r.success, r.error
    assert client.edits() == ["100"]


def test_sqw_longer_story_never_uses_the_whole_story_file_as_chapter_one(story, poster_for, tmp_path):
    client = Stand()
    file = tmp_path / "whole.html"
    file.write_text("<p>whole</p>", encoding="utf-8")
    package = _package("sqw")
    package.file_path = str(file)
    r = asyncio.run(poster_for("sqw", client, missing={1}).edit(WORK, package))
    assert not r.success and "Chapter 1" in r.error
    assert client.edits() == []


def test_sf_empty_content_list_uploads_nothing(story, poster_for):
    client = Stand(count=0)
    r = _edit(poster_for("sf", client), "sf")
    assert not r.success and "chapter list" in r.error
    assert client.writes == []


def test_sf_titles_only_update_reports_a_failed_title_after_trying_them_all(story, poster_for):
    client = Stand()
    client.fail_on.add("101")
    r = _edit(poster_for("sf", client), "sf", skip_content_refresh=True)
    assert not r.success and "Chapter 2" in r.error
    assert ("title", "102") in client.writes


def test_ao3_rejected_post_chapter_is_a_failure_not_already_posted(monkeypatch):
    client = AO3Client.__new__(AO3Client)
    client._logged_in = True
    page = '<form action="/works/900/chapters/5/post"><input name="authenticity_token" value="t"></form>'

    async def get(url, **k):
        return page

    async def post(url, **k):
        return NS(status_code=500, text="")

    async def chapters(work):
        return [{"chapter_id": "5", "index": 1, "title": "One"}]

    monkeypatch.setattr(client, "_get_page", get)
    monkeypatch.setattr(client, "_post_with_retry", post)
    monkeypatch.setattr(client, "get_chapter_ids", chapters)
    monkeypatch.setattr(client, "_polite_delay", _none)
    ps = asyncio.run(client.publish_all_draft_chapters(WORK))
    assert ps["already_posted"] == 0 and [cid for cid, _ in ps["failed"]] == ["5"]


def _failed_update_rows(monkeypatch, name, extras):
    """Run the manager's real update path against a site that fails; what got queued to run again."""
    conn = get_connection()
    try:
        pq.upsert_publication(conn, name, 0, "sf", external_id=WORK, status="posted")
    finally:
        conn.close()

    async def edit(ext_id, package):
        return PostResult(success=False, error="HTTP 503")

    package = StoryUploadPackage(story_name=name, chapter_index=0, chapter_title="", platform="sf",
                                 title="Sample Story", description="", file_type="html")
    monkeypatch.setattr(manager, "_get_poster",
                        lambda *a: NS(requires_mode="any", supports_edit=True, edit=edit, _rate_limit=_none))
    monkeypatch.setattr(story_reader, "load_story", lambda n: NS(name=n))
    monkeypatch.setattr(story_reader, "build_package", lambda *a, **k: package)
    results = asyncio.run(manager._update_story(name, ["sf"], [0], extras, None, run=None))
    assert results and not results[0]["success"]
    conn = get_connection()
    try:
        return [r[0] for r in conn.execute(
            "SELECT draft FROM posting_queue WHERE story_name = ? AND action = 'update'", (name,))]
    finally:
        conn.close()


def test_failed_details_only_update_is_not_retried_as_a_full_one(monkeypatch):
    assert _failed_update_rows(monkeypatch, "Updatefail_Details", {"skip_content_refresh": True}) == []


def test_failed_full_update_is_retried_and_keeps_the_live_choice(monkeypatch):
    assert _failed_update_rows(monkeypatch, "Updatefail_Full", {"draft": False}) == [0]


def test_failed_full_update_with_no_choice_stays_no_choice(monkeypatch):
    assert _failed_update_rows(monkeypatch, "Updatefail_Plain", None) == [None]


def test_the_scheduler_gives_an_update_retry_its_draft_choice():
    calls = [c for c in ast.walk(ast.parse(inspect.getsource(scheduler)))
             if isinstance(c, ast.Call) and getattr(c.func, "attr", "") == "update_story"]
    assert calls and all("extras" in {k.arg for k in c.keywords} for c in calls)


def test_a_missing_file_is_not_retried():
    assert not manager._schedule_retry("Updatefail_Missing", 1, "sf", "update",
                                       "Chapter 2 didn't update: its file wasn't found")


def test_sf_failed_title_refresh_fails_the_update(story, poster_for):
    client = Stand()
    poster = poster_for("sf", client)

    async def titles(*a, **k):
        raise RuntimeError("no titles")

    poster._set_chapter_titles = titles
    r = _edit(poster, "sf")
    assert not r.success and "no titles" in r.error


def test_ao3_resume_stops_when_it_cant_see_the_chapters(story, poster_for, monkeypatch):
    """A half-finished post must not add every chapter to a work that already has some."""
    conn = get_connection()
    try:
        pq.upsert_publication(conn, STORY, 0, "ao3", external_id=WORK, status="draft")
    finally:
        conn.close()
    client = Stand()
    client.list_error = RuntimeError("Couldn't read the list")
    poster = poster_for("ao3", client)

    async def exists(work):
        return True

    async def resume(client_, work, rating, skin):
        return rating

    monkeypatch.setattr(poster, "probe_exists", exists)
    monkeypatch.setattr(poster, "_resume_edit", resume)
    r = asyncio.run(poster.post(_package("ao3")))
    assert not r.success and r.external_id == WORK, r.error
    assert client.writes == []


@pytest.mark.parametrize("cls", [AO3Client, SquidgeWorldClient])
@pytest.mark.parametrize("page", [None, "<html>nothing here</html>"])
def test_chapter_list_unreadable_raises(monkeypatch, cls, page):
    client = cls.__new__(cls)

    async def get(url, **k):
        return page

    monkeypatch.setattr(client, "_get_page", get)
    with pytest.raises(RuntimeError, match="chapter list"):
        asyncio.run(client.get_chapter_ids(WORK))


@pytest.mark.parametrize("cls", [AO3Client, SquidgeWorldClient])
def test_chapter_list_still_parses(monkeypatch, cls):
    client = cls.__new__(cls)

    async def get(url, **k):
        return '<a href="/works/900/chapters/5">1. One</a><a href="/works/900/chapters/6">2. Two</a>'

    monkeypatch.setattr(client, "_get_page", get)
    assert [c["chapter_id"] for c in asyncio.run(client.get_chapter_ids(WORK))] == ["5", "6"]


def test_sf_content_ids_raise_when_the_submission_is_unreadable(monkeypatch):
    client = SoFurryClient.__new__(SoFurryClient)

    async def gone(self, sid):
        return {}

    monkeypatch.setattr(SoFurryClient, "get_submission", gone)
    with pytest.raises(SoFurryError):
        asyncio.run(client.get_content_ids("1"))


def test_sf_content_ids_pass_on_an_api_error(monkeypatch):
    client = SoFurryClient.__new__(SoFurryClient)

    async def broken(self, sid):
        raise SoFurryError("HTTP 500")

    monkeypatch.setattr(SoFurryClient, "get_submission", broken)
    with pytest.raises(SoFurryError):
        asyncio.run(client.get_content_ids("1"))


def test_sf_content_ids_stay_a_list_of_strings(monkeypatch):
    client = SoFurryClient.__new__(SoFurryClient)

    async def read(self, sid):
        return {"content": [{"contentId": 7}, {"id": "8"}, {}]}

    monkeypatch.setattr(SoFurryClient, "get_submission", read)
    assert asyncio.run(client.get_content_ids("1")) == ["7", "8"]
