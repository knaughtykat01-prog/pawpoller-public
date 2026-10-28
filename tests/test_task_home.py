"""Spec 053 (task home + schedule chapters): the backend half.

- A scheduled story post's public/draft choice is stored and reaches the poster; rows from
  before 053 (NULL) keep posting with no extras, exactly as they did.
- The drip's `only_new` skips chapter × site already posted or waiting, `dry_run` stores
  nothing, and chapters shared by two sites share a slot.
- `home_screen` is a validated preference, Start by default.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from database.db import get_connection
from database import posting_queries


# ── Fakes: a 4-chapter story and posters that accept anything ─────────────

@pytest.fixture()
def story(monkeypatch, tmp_path):
    from routes import editor_api
    from posting import manager, story_reader
    monkeypatch.setattr(editor_api, "get_archive_path", lambda: tmp_path)
    monkeypatch.setattr(editor_api, "_resolve_story_dir", lambda name: tmp_path / "Sample_Story")
    monkeypatch.setattr(story_reader, "load_story", lambda name: SimpleNamespace(total_chapters=4, title="Sample"))
    monkeypatch.setattr(story_reader, "build_package", lambda s, ch, p: SimpleNamespace(ch=ch))
    monkeypatch.setattr(manager, "_get_poster",
                        lambda p: SimpleNamespace(validate=lambda pkg: [], requires_mode="any"))
    monkeypatch.setattr(editor_api, "_store_comments", lambda *a, **k: None)
    return "Sample_Story"


def _drip(**kw):
    from routes import editor_api
    body = dict(platforms=["ib"], start="2031-01-06T09:00:00+00:00", interval_days=7)
    body.update(kw)
    return asyncio.run(editor_api.drip_schedule("Sample_Story", editor_api.DripRequest(**body)))


def _rows():
    conn = get_connection()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT chapter_index, platform, scheduled_at, draft FROM posting_queue ORDER BY queue_id")]
    finally:
        conn.close()


def _posted(ch, platform):
    conn = get_connection()
    try:
        conn.execute("INSERT INTO publications (story_name, chapter_index, platform, content_type, status) "
                     "VALUES ('Sample_Story', ?, ?, 'story', 'posted')", (ch, platform))
        conn.commit()
    finally:
        conn.close()


# ── Drip: only_new / dry_run / draft ──────────────────────────────────────

def test_only_new_skips_posted_and_scheduled_and_counts_slots_from_what_goes_out(story):
    _posted(1, "ib")
    conn = get_connection()
    try:
        posting_queries.add_to_queue(conn, "Sample_Story", 2, "ib", scheduled_at="2031-01-01 09:00:00")
    finally:
        conn.close()
    out = _drip(only_new=True, draft=False)
    assert [s["chapter"] for s in out["slots"]] == [3, 4]
    # Chapter 3 takes the first slot, not chapter 1's.
    assert out["slots"][0]["scheduled_at"] == "2031-01-06 09:00:00"
    assert {(s["chapter"], s["reason"]) for s in out["skipped"]} == {(1, "posted"), (2, "scheduled")}
    new = [r for r in _rows() if r["chapter_index"] in (3, 4)]
    assert len(new) == 2 and all(r["draft"] == 0 for r in new)


def test_two_sites_share_a_slot_and_a_chapter_on_one_goes_only_to_the_other(story):
    _posted(1, "ib")
    out = _drip(platforms=["ib", "sf"], only_new=True, dry_run=True)
    assert out["slots"][0] == {"chapter": 1, "scheduled_at": "2031-01-06 09:00:00", "platforms": ["sf"]}
    assert out["slots"][1]["platforms"] == ["ib", "sf"]
    assert out["rows"] == 7


def test_dry_run_stores_nothing(story):
    out = _drip(dry_run=True)
    assert out["drip_group"] is None and len(out["slots"]) == 4
    assert _rows() == []


def test_nothing_left_is_a_plain_refusal(story):
    for ch in range(1, 5):
        _posted(ch, "ib")
    with pytest.raises(HTTPException) as e:
        _drip(only_new=True)
    assert e.value.status_code == 400 and "already" in e.value.detail


def test_interval_in_minutes_steps_real_time_and_whole_days_keep_the_wall_clock(story):
    """The sheet's "every" list runs from 1 minute to 28 days (operator, 23 Oct)."""
    r = _drip(interval_minutes=30, dry_run=True)
    assert [s["scheduled_at"] for s in r["slots"]][:3] == ["2031-01-06 09:00:00", "2031-01-06 09:30:00", "2031-01-06 10:00:00"]
    r = _drip(interval_minutes=2880, dry_run=True)
    assert [s["scheduled_at"][:10] for s in r["slots"]][:3] == ["2031-01-06", "2031-01-08", "2031-01-10"]


@pytest.mark.parametrize("bad", [0, 40321])
def test_interval_minutes_out_of_range_is_refused(story, bad):
    with pytest.raises(HTTPException):
        _drip(interval_minutes=bad, dry_run=True)


def _refuse_chapter(monkeypatch, bad_ch):
    from posting import manager
    monkeypatch.setattr(manager, "_get_poster", lambda p: SimpleNamespace(
        validate=lambda pkg: ["Inkbunny requires at least 4 tags (got 1)"] if pkg.ch == bad_ch else [],
        requires_mode="any"))


def test_a_preview_names_each_refused_pair_instead_of_refusing(story, monkeypatch):
    """Amendment A: step 1 says which site would refuse which chapter, and why."""
    _refuse_chapter(monkeypatch, 2)
    r = _drip(dry_run=True)
    assert r["ok"] is False and len(r["slots"]) == 4
    assert r["problems"] == [{"chapter": 2, "platform": "ib", "message": "Inkbunny requires at least 4 tags (got 1)"}]
    with pytest.raises(HTTPException):          # the real run still never half-books
        _drip()
    assert _rows() == []


def test_a_refused_chapter_left_off_one_site_lets_the_rest_go(story, monkeypatch):
    """WIREFLOW job 5: one chapter a site refuses no longer holds the whole run — leave just that
    pair out and the other chapters (and sites) still schedule."""
    _refuse_chapter(monkeypatch, 2)
    r = _drip(skip=["2:ib"], dry_run=True)
    assert r["problems"] == [] and r["rows"] == 3
    assert {"chapter": 2, "platform": "ib", "reason": "left out"} in r["skipped"]
    _drip(skip=["2:ib"])
    assert sorted(x["chapter_index"] for x in _rows()) == [1, 3, 4]


def test_skip_ignores_pairs_it_does_not_know(story):
    r = _drip(skip=["9:ib", "1:zz", "x:ib", "", "1", "²:ib", "9" * 5000 + ":ib"], dry_run=True)
    assert r["rows"] == 4


def test_leaving_every_chapter_out_is_a_plain_refusal(story):
    with pytest.raises(HTTPException):
        _drip(skip=["1:ib", "2:ib", "3:ib", "4:ib"], dry_run=True)


def test_a_moved_chapter_keeps_its_own_time_and_the_rest_keep_theirs(story):
    r = _drip(moves={2: "2031-01-20T18:30:00+00:00"})
    got = {row["chapter_index"]: row["scheduled_at"] for row in _rows()}
    assert got[2] == "2031-01-20 18:30:00"
    assert got[1][:10] == "2031-01-06" and got[3][:10] == "2031-01-20" and got[4][:10] == "2031-01-27"
    assert r["rows"] == 4


def test_a_chapter_moved_into_the_past_is_refused(story):
    with pytest.raises(HTTPException):
        _drip(moves={1: "2001-01-01T00:00:00+00:00"}, dry_run=True)

def test_a_site_listed_twice_is_still_one_row_per_chapter(story):
    """4.86.0 security review (Low): duplicates in `platforms` booked a chapter twice."""
    _posted(1, "ib")
    r = _drip(platforms=["ib", "ib"], only_new=True)
    assert r["rows"] == 3 and sorted(x["chapter_index"] for x in _rows()) == [2, 3, 4]


def test_the_grid_drip_leaves_each_sites_draft_default_alone():
    """4.86.0 security review (Medium): the grid's Drip has no visibility control, so it must not
    send draft: false — that would turn AO3's draft default into public chapters."""
    src = (Path(__file__).resolve().parent.parent / "frontend/js/publish_check.js").read_text(encoding="utf-8")
    assert "only_new: true, draft: null," in src and "draft: false," not in src

def test_never_post_accounts_are_not_offered_in_post_as():
    """FRIENDGUARD in the shared picker: a persona holding only someone else's account gets no chip."""
    src = (Path(__file__).resolve().parent.parent / "frontend/js/components.js").read_text(encoding="utf-8")
    assert "a.enabled && !a.never_post && offered.has(a.platform)" in src

def test_old_callers_get_the_old_drip(story):
    _posted(1, "ib")
    out = _drip()
    assert len(out["slots"]) == 4 and out["skipped"] == []
    assert all(r["draft"] is None for r in _rows())


# ── Single schedule stores the choice ──────────────────────────────────────

def test_single_schedule_stores_draft_only_when_given(story):
    from routes import editor_api
    for draft in (True, None):
        asyncio.run(editor_api.schedule_publish("Sample_Story", editor_api.ScheduleRequest(
            platform="ib", chapter=2, scheduled_at="2031-01-06T09:00:00+00:00", draft=draft)))
    assert [r["draft"] for r in _rows()] == [1, None]


# ── The scheduler passes it through ────────────────────────────────────────

@pytest.mark.parametrize("stored, extras", [(None, None), (0, {"draft": False}), (1, {"draft": True})])
def test_scheduler_hands_the_choice_to_the_poster(monkeypatch, stored, extras):
    from posting import manager, scheduler
    seen = {}

    async def fake_post_story(name, platforms, chapters, extras=None, **kw):
        seen["extras"] = extras
        return [{"platform": "ib", "success": True, "url": "https://example.com/s/1"}]

    monkeypatch.setattr(manager, "post_story", fake_post_story)
    conn = get_connection()
    try:
        qid = posting_queries.add_to_queue(conn, "Sample_Story", 1, "ib",
                                           draft=None if stored is None else bool(stored))
        item = conn.execute("SELECT * FROM posting_queue WHERE queue_id = ?", (qid,)).fetchone()
    finally:
        conn.close()
    asyncio.run(scheduler._process_queue_item_core(item))
    assert seen["extras"] == extras


# ── Upgrade path ───────────────────────────────────────────────────────────

def test_draft_column_is_added_to_a_pre_053_queue():
    from database.db import init_db
    conn = get_connection()
    try:
        # The real table as it was before 053: today's, minus the new column.
        conn.executescript("""
            ALTER TABLE posting_queue DROP COLUMN draft;
            INSERT INTO posting_queue (story_name, platform) VALUES ('Old_Story', 'ib');
        """)
        conn.commit()
    finally:
        conn.close()
    init_db()
    conn = get_connection()
    try:
        assert conn.execute("SELECT draft FROM posting_queue WHERE story_name='Old_Story'").fetchone()[0] is None
    finally:
        conn.close()


# ── home_screen ────────────────────────────────────────────────────────────

def test_home_screen_defaults_to_start_and_is_validated():
    import config
    from routes import api
    assert api.get_preferences()["home_screen"] == "start"
    api.save_preferences({"home_screen": "overview"})
    assert config.get_settings()["home_screen"] == "overview"
    assert api.get_preferences()["home_screen"] == "overview"
    with pytest.raises(HTTPException):
        api.save_preferences({"home_screen": "laurels"})
    assert config.get_settings()["home_screen"] == "overview"
