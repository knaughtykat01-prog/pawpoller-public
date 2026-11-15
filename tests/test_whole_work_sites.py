"""TGAO3DUP: a post with no chapters named sends AO3, SquidgeWorld and SoFurry the whole work once.

Those sites post a whole work in one go (their poster creates every chapter itself). Telegram's /upload and the
API's post with no `chapters` asked the shared loop for "all chapters", which ran the poster once per chapter: a
2-chapter story became two copies of the work. Per-chapter sites still get one post per chapter.
"""
import pytest

import config
from database import posting_queries
from database.db import get_connection, init_db
from posting import manager
from posting.platforms.base import PostResult


class _StubPoster:
    requires_mode = "any"

    def __init__(self, code):
        self.platform_id = code
        self.posted = []

    def validate(self, package):
        return []

    async def post(self, package):
        self.posted.append(package.chapter_index)
        n = len(self.posted)
        return PostResult(success=True, external_id=f"{self.platform_id}{n}",
                          external_url=f"https://example.org/{self.platform_id}/{n}")

    async def _rate_limit(self):
        pass


@pytest.fixture(autouse=True)
def clean_db():
    init_db()
    conn = get_connection()
    for t in ("posting_log", "posting_queue", "publications"):
        conn.execute(f"DELETE FROM {t}")
    conn.commit()
    conn.close()


@pytest.fixture
def stubs(story_archive, monkeypatch):
    config.save_settings({"posting_story_archive_path": str(story_archive)})
    made = {}
    monkeypatch.setattr(manager, "_get_poster",
                        lambda plat, acct=None: made.setdefault(plat, _StubPoster(plat)))
    return made


@pytest.mark.asyncio
async def test_no_chapters_named_posts_a_whole_work_site_once(stubs):
    results = await manager.post_story("Test_Story", ["ao3", "sqw", "sf", "ib"])   # Test_Story has 2 chapters
    for code in ("ao3", "sqw", "sf"):
        assert stubs[code].posted == [0], code            # the whole work, once
    assert stubs["ib"].posted == [1, 2]                   # a per-chapter site is unchanged
    assert sorted((r["platform"], r["chapter_index"]) for r in results) == [
        ("ao3", 0), ("ib", 1), ("ib", 2), ("sf", 0), ("sqw", 0)]
    conn = get_connection()
    try:
        ao3 = [p["chapter_index"] for p in posting_queries.get_publications(conn, story_name="Test_Story", platform="ao3")]
    finally:
        conn.close()
    assert ao3 == [0]                                     # where AO3's own resume looks for the work


@pytest.mark.asyncio
async def test_chapters_named_by_the_caller_are_left_as_asked(stubs):
    await manager.post_story("Test_Story", ["ao3"], chapters=[0])
    assert stubs["ao3"].posted == [0]


def test_the_grid_and_the_loop_share_one_list():
    assert manager.WORK_ORIENTED == frozenset({"ao3", "sqw", "sf"})
