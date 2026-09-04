"""The two endpoints the story board adds (4.5.0, C2 spec §6.6).

1. GET /api/editor/stories/{name}/tag-preview — what each site gets, for a
   story. ⚠ It must NOT reuse masterpieces_api._PREVIEW_PLATFORMS: that is the
   art list (e621, Itaku and Furbooru take no stories) and it omits AO3 and
   Wattpad, the two whose tag limits actually bite a story.

2. POST /api/posting/stories/{name}/link-url — record a copy posted by hand,
   the story twin of the artwork route. Preview by default, confirm to write.
   ⚠ chapter_index has no safe default: upsert_publication would file chapter
   7 as the whole work, and re-linking a chapter that already exists updates
   silently and reads as success — so both are refused.
"""
from __future__ import annotations

import json

import pytest
from fastapi import HTTPException

from routes import editor_api, posting_api


# ── tag-preview ──────────────────────────────────────────────────────────────

@pytest.fixture()
def story(tmp_path, monkeypatch):
    d = tmp_path / "Sample_Story"
    d.mkdir()
    (d / "story.json").write_text(json.dumps({
        "title": "Sample Story",
        "tags": {"default": [f"tag_{i}" for i in range(80)], "wattpad": ["one", "two"]},
    }), encoding="utf-8")
    monkeypatch.setattr(editor_api, "get_archive_path", lambda: tmp_path)
    return "Sample_Story"


class TestTagPreview:
    @pytest.mark.asyncio
    async def test_the_story_platforms_not_the_art_ones(self, story):
        out = await editor_api.story_tag_preview(story)
        codes = [p["platform"] for p in out["platforms"]]
        assert "ao3" in codes and "wp" in codes, "the two whose limits bite a story"
        for art_only in ("e621", "ik", "fbr"):
            assert art_only not in codes, f"{art_only} takes no stories"
        assert tuple(codes) == editor_api._STORY_PREVIEW_PLATFORMS

    @pytest.mark.asyncio
    async def test_the_shape_matches_the_art_route_so_one_renderer_serves_both(self, story):
        out = await editor_api.story_tag_preview(story)
        assert set(out) == {"name", "canonical", "core_count", "platforms"}
        assert out["core_count"] == 0, "a story has no core/auxiliary split"
        row = out["platforms"][0]
        assert set(row) >= {"platform", "limit", "sent", "total", "dropped", "override"}

    @pytest.mark.asyncio
    async def test_ao3s_75_cap_trims_and_says_what_it_cut(self, story):
        out = await editor_api.story_tag_preview(story)
        ao3 = next(p for p in out["platforms"] if p["platform"] == "ao3")
        assert ao3["total"] == 80 and ao3["sent"] == 75 and len(ao3["dropped"]) == 5
        assert ao3["dropped"] == [f"tag_{i}" for i in range(75, 80)], "trimmed from the tail"

    @pytest.mark.asyncio
    async def test_an_override_is_reported_verbatim_under_its_long_key_too(self, story):
        """story.json may name a site by its long key ("wattpad"); the row is
        still keyed by the code and is not re-trimmed behind the user's back."""
        out = await editor_api.story_tag_preview(story)
        wp = next(p for p in out["platforms"] if p["platform"] == "wp")
        assert wp["override"] is True and wp["sent"] == 2 and wp["dropped"] == []

    @pytest.mark.asyncio
    async def test_a_story_without_a_json_is_a_404(self, tmp_path, monkeypatch):
        (tmp_path / "Bare").mkdir()
        monkeypatch.setattr(editor_api, "get_archive_path", lambda: tmp_path)
        with pytest.raises(HTTPException) as e:
            await editor_api.story_tag_preview("Bare")
        assert e.value.status_code == 404


# ── link-url ─────────────────────────────────────────────────────────────────

class _Conn:
    """Just enough of a connection for the route: one existing publication
    for (fa, chapter 2), a masterpiece owning nothing, commit/close no-ops."""
    def __init__(self):
        self.committed = False

    def execute(self, sql, params=()):
        class _R:
            def __init__(self, row): self._row = row
            def fetchone(self): return self._row
        if "FROM publications" in sql and "story_name = ?" in sql:
            story, plat, ch = params[0], params[1], params[2]
            return _R({"pub_id": 9} if (plat == "fa" and int(ch) == 2 and story == "Sample_Story") else None)
        if "FROM publications" in sql and "external_id = ?" in sql:
            plat, sid = params
            return _R({"story_name": "Sample_Story", "chapter_index": 2} if (plat == "fa" and sid == "222") else None)
        if "FROM masterpiece_members" in sql:
            return _R(None)
        return _R(None)

    def commit(self): self.committed = True
    def close(self): pass


class _Story:
    name = "Sample_Story"
    total_chapters = 3


@pytest.fixture()
def link(monkeypatch):
    conn = _Conn()
    writes = []
    monkeypatch.setattr(posting_api, "get_connection", lambda: conn)
    from posting import story_reader
    monkeypatch.setattr(story_reader, "load_story", lambda name: _Story())
    from database import posting_queries
    monkeypatch.setattr(posting_queries, "upsert_publication",
                        lambda c, story, ch, plat, **kw: writes.append((story, ch, plat, kw)) or 41)
    from database import collections_queries
    monkeypatch.setattr(collections_queries, "_submission_row",
                        lambda c, plat, sid: {"title": "Seen", "account_id": 3} if sid == "111" else {})
    return conn, writes


FA_URL = "https://www.furaffinity.net/view/111/"


class TestLinkByUrl:
    def test_previews_by_default_and_reports_the_chapter_count(self, link):
        conn, writes = link
        r = posting_api.link_story_by_url("Sample_Story", {"url": FA_URL})
        assert r["status"] == "preview" and r["total_chapters"] == 3
        assert writes == [] and not conn.committed
        c = r["candidates"][0]
        assert c["platform"] == "fa" and c["submission_id"] == "111" and c["known"] is True

    def test_the_preview_says_when_the_url_is_already_recorded(self, link):
        r = posting_api.link_story_by_url("Sample_Story", {"url": "https://www.furaffinity.net/view/222/"})
        c = r["candidates"][0]
        assert c["publication_of"] == "Sample_Story" and c["publication_chapter"] == 2

    def test_confirm_needs_a_chapter_index(self, link):
        with pytest.raises(HTTPException) as e:
            posting_api.link_story_by_url("Sample_Story", {"url": FA_URL, "confirm": True})
        assert e.value.status_code == 400 and "chapter_index" in e.value.detail

    def test_confirm_records_the_chapter_it_was_told(self, link):
        conn, writes = link
        r = posting_api.link_story_by_url("Sample_Story", {"url": FA_URL, "confirm": True, "chapter_index": 1})
        assert r["status"] == "linked" and r["chapter_index"] == 1 and r["pub_id"] == 41
        story, ch, plat, kw = writes[0]
        assert (story, ch, plat) == ("Sample_Story", 1, "fa")
        assert kw["external_id"] == "111" and kw["external_url"] == FA_URL
        assert kw["content_type"] == "story" and kw["status"] == "posted" and kw["account_id"] == 3
        assert conn.committed

    def test_confirm_records_the_current_files_hash_so_the_row_is_not_drifted(self, link, tmp_path, monkeypatch):
        """detect_changes treats a missing hash as changed (a claim could be
        anything); a copy linked right after posting by hand is the file on
        disk, so its hash is recorded."""
        from posting import story_reader, sync as posting_sync
        f = tmp_path / "ch1.txt"
        f.write_text("chapter one", encoding="utf-8")
        monkeypatch.setattr(story_reader, "_resolve_format_file", lambda s, ch, plat: (str(f), "txt"))
        conn, writes = link
        posting_api.link_story_by_url("Sample_Story", {"url": FA_URL, "confirm": True, "chapter_index": 1})
        kw = writes[0][3]
        assert kw["file_hash"] == posting_sync.hash_file(str(f)) and kw["format_file"] == "txt"

    def test_confirm_without_a_resolvable_file_still_links(self, link):
        conn, writes = link
        r = posting_api.link_story_by_url("Sample_Story", {"url": FA_URL, "confirm": True, "chapter_index": 1})
        assert r["status"] == "linked" and writes[0][3]["file_hash"] == ""

    def test_a_chapter_beyond_the_story_is_refused(self, link):
        with pytest.raises(HTTPException) as e:
            posting_api.link_story_by_url("Sample_Story", {"url": FA_URL, "confirm": True, "chapter_index": 7})
        assert e.value.status_code == 400

    def test_relinking_an_existing_chapter_is_refused_not_silently_updated(self, link):
        """upsert_publication would UPDATE the row and return success."""
        conn, writes = link
        with pytest.raises(HTTPException) as e:
            posting_api.link_story_by_url("Sample_Story", {"url": "https://www.furaffinity.net/view/222/",
                                                           "confirm": True, "chapter_index": 2})
        assert e.value.status_code == 409 and writes == []

    def test_an_unrecognised_link_is_a_422_that_names_the_sites(self, link):
        with pytest.raises(HTTPException) as e:
            posting_api.link_story_by_url("Sample_Story", {"url": "https://example.com/nothing"})
        assert e.value.status_code == 422

    def test_nothing_at_all_is_a_400(self, link):
        with pytest.raises(HTTPException) as e:
            posting_api.link_story_by_url("Sample_Story", {})
        assert e.value.status_code == 400
