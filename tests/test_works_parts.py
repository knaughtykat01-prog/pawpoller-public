"""Library: one card per piece (4.47.0, spec 014).

The Library and the Masterpieces tab showed the same pieces twice, and every
extra version of an artwork was its own card. Now each piece is one card that
carries what both showed, and its versions (art) or chapters (stories) fan out
from it. These tests pin the data the cards are drawn from:

  * the card's numbers pool EVERY upload of the piece — member rows (what the
    Masterpieces tab pooled) and posted publications (what the Library pooled),
    de-duplicated, because on the live catalogue 46 of 189 posted artwork
    publications had no member row;
  * each version carries its own sites, numbers and status;
  * each chapter of a story does too, with titles from the story's chapter list;
  * scheduled posts reach the right card, version and chapter.
"""
from __future__ import annotations

import pytest

from database import masterpiece_queries as mq
from database import platform_metrics
from database import posting_queries
from database.db import get_connection
from routes import submissions_api as sa


# ── pooling: members + publications, per version ─────────────────────────────

def _ib_row(views, faves, comments):
    spec = platform_metrics.get("ib")
    return {spec.views: views, spec.faves: faves, spec.comments: comments,
            "title": "", "link": "", "thumbnail_url": ""}


@pytest.fixture
def rows(monkeypatch):
    table = {}
    monkeypatch.setattr(mq, "_submission_rows_bulk",
                        lambda conn, pairs: {p: table[p] for p in pairs if p in table})
    return table


def test_an_upload_known_twice_counts_once(rows):
    rows[("ib", "1")] = _ib_row(100, 10, 1)
    conn = get_connection()
    try:
        mq.add_member(conn, "Piece", "ib", "1")
        conn.commit()
        out = mq.summarize_many(conn, ["Piece"], extra={"Piece": [
            {"platform": "ib", "submission_id": "1", "account_id": None, "variant_key": "", "url": ""}]})
    finally:
        conn.close()
    assert out["Piece"]["totals"]["views"] == 100
    assert out["Piece"]["totals"]["locations"] == 1


def test_a_publication_without_a_member_row_still_counts(rows):
    """The Library card must not lose numbers the old Library card had."""
    rows[("ib", "1")] = _ib_row(100, 10, 1)
    rows[("ib", "2")] = _ib_row(50, 5, 0)
    conn = get_connection()
    try:
        mq.add_member(conn, "Piece", "ib", "1")
        conn.commit()
        out = mq.summarize_many(conn, ["Piece"], extra={"Piece": [
            {"platform": "ib", "submission_id": "2", "account_id": None, "variant_key": "", "url": ""}]})
    finally:
        conn.close()
    assert out["Piece"]["totals"]["views"] == 150
    assert out["Piece"]["totals"]["favorites"] == 15


def test_numbers_split_by_version(rows):
    rows[("ib", "1")] = _ib_row(100, 10, 1)
    rows[("ib", "2")] = _ib_row(40, 4, 0)
    conn = get_connection()
    try:
        mq.add_member(conn, "Piece", "ib", "1")
        mq.add_member(conn, "Piece", "ib", "2", variant_key="nsfw")
        conn.commit()
        out = mq.summarize_many(conn, ["Piece"])
    finally:
        conn.close()
    bv = out["Piece"]["by_variant"]
    assert bv[""]["totals"]["views"] == 100
    assert bv["nsfw"]["totals"]["views"] == 40
    assert bv["nsfw"]["platforms"] == ["ib"]
    assert out["Piece"]["totals"]["views"] == 140


def test_a_members_version_wins_over_a_publications(rows):
    rows[("ib", "2")] = _ib_row(40, 4, 0)
    conn = get_connection()
    try:
        mq.add_member(conn, "Piece", "ib", "2", variant_key="nsfw")
        conn.commit()
        out = mq.summarize_many(conn, ["Piece"], extra={"Piece": [
            {"platform": "ib", "submission_id": "2", "account_id": None, "variant_key": "", "url": ""}]})
    finally:
        conn.close()
    assert set(out["Piece"]["by_variant"]) == {"nsfw"}


def test_the_masterpiece_list_shape_is_unchanged_apart_from_by_variant(rows):
    conn = get_connection()
    try:
        out = mq.summarize_many(conn, ["Empty"])
    finally:
        conn.close()
    assert set(out["Empty"]) == {"totals", "persona_ids", "member_count", "platforms",
                                 "cover_thumb", "cover_platform", "by_variant"}
    assert out["Empty"]["by_variant"] == {}


# ── scheduled lookup ─────────────────────────────────────────────────────────

def _queue(conn, name, *, ct="artwork", ch=0, vk="", at="2026-10-03 10:00:00", status="pending"):
    conn.execute(
        "INSERT INTO posting_queue (story_name, chapter_index, platform, action, variant_key, "
        "scheduled_at, status, content_type) VALUES (?,?,?,?,?,?,?,?)",
        (name, ch, "ib", "post", vk, at, status, ct))


def test_scheduled_parts_groups_and_takes_the_earliest():
    conn = get_connection()
    try:
        _queue(conn, "Piece", at="2026-10-05 10:00:00")
        _queue(conn, "Piece", at="2026-10-03 10:00:00")
        _queue(conn, "Piece", vk="nsfw", at="2026-10-04 10:00:00")
        _queue(conn, "Piece", at="2026-10-01 10:00:00", status="completed")
        _queue(conn, "Tale", ct="story", ch=3, at="2026-10-06 09:00:00")
        conn.commit()
        out = posting_queries.scheduled_parts(conn)
    finally:
        conn.close()
    art = out[("artwork", "Piece")]
    assert {(r["variant_key"], r["scheduled_at"]) for r in art} == {
        ("", "2026-10-03 10:00:00"), ("nsfw", "2026-10-04 10:00:00")}
    assert out[("story", "Tale")] == [
        {"chapter_index": 3, "variant_key": "", "scheduled_at": "2026-10-06 09:00:00"}]


# ── assemble_works: the cards ────────────────────────────────────────────────

def _art(name="Piece", variants=True):
    return {"name": name, "title": name, "rating": "general", "image": "a.png",
            "variants": ([{"key": "", "label": "Main", "image": "a.png"},
                          {"key": "nsfw", "label": "NSFW", "image": "b.png", "rating": "adult"}]
                         if variants else []),
            "tags": {}, "artist": {"name": "Inkwolf"}}


def _works(**kw):
    base = dict(stories=[], artworks=[], pubs=[], acct_to_persona={}, personas={})
    base.update(kw)
    return sa.assemble_works(**base)["works"]


def test_one_card_per_piece_with_version_state():
    roll = {"Piece": {"totals": {"views": 140, "favorites": 14, "comments": 1},
                      "platforms": ["ib"], "persona_ids": [],
                      "by_variant": {"": {"totals": {"views": 100, "favorites": 10, "comments": 1},
                                          "platforms": ["ib"]}}}}
    sched = {("artwork", "Piece"): [{"chapter_index": 0, "variant_key": "nsfw",
                                     "scheduled_at": "2026-10-03 10:00:00"}]}
    ws = _works(artworks=[_art()], rollups=roll, scheduled=sched)
    assert len(ws) == 1
    w = ws[0]
    assert w["stats"]["views"] == 140                      # pooled like Masterpieces
    assert w["platforms"] == ["ib"]
    assert w["status"] == {"state": "live", "label": "Live on 1", "next_at": "2026-10-03 10:00:00"}
    assert w["main"]["status"] == "live" and w["main"]["stats"]["views"] == 100
    [v] = w["variants"]                                    # only the non-primary version is a tile
    assert v["key"] == "nsfw"
    assert v["status"] == "scheduled" and v["scheduled_at"] == "2026-10-03 10:00:00"
    assert v["platforms"] == [] and v["stats"]["views"] == 0
    assert v["detail_route"].endswith("?v=nsfw")


def test_platforms_are_the_union_of_publications_and_members():
    pubs = [{"content_type": "artwork", "story_name": "Piece", "platform": "fa",
             "status": "posted", "account_id": None, "stats": {}}]
    roll = {"Piece": {"totals": {}, "platforms": ["ib"], "persona_ids": [], "by_variant": {}}}
    [w] = _works(artworks=[_art(variants=False)], pubs=pubs, rollups=roll)
    assert w["platforms"] == ["fa", "ib"]
    assert w["status"]["label"] == "Live on 2"


def test_a_piece_with_nothing_is_a_draft_and_scheduled_wins_over_draft():
    [w] = _works(artworks=[_art(variants=False)])
    assert w["status"]["state"] == "draft"
    [w] = _works(artworks=[_art(variants=False)], scheduled={("artwork", "Piece"): [
        {"chapter_index": 0, "variant_key": "", "scheduled_at": "2026-10-03 10:00:00"}]})
    assert w["status"] == {"state": "scheduled", "label": "Scheduled", "next_at": "2026-10-03 10:00:00"}


def test_without_rollups_the_card_keeps_the_publication_numbers():
    """assemble_works is called by tests and by anything that has no rollup; the
    old behaviour must survive there."""
    pubs = [{"content_type": "artwork", "story_name": "Piece", "platform": "ib",
             "status": "posted", "account_id": None,
             "stats": {"views": 7, "faves": 2, "comments": 0}}]
    [w] = _works(artworks=[_art(variants=False)], pubs=pubs)
    assert w["stats"]["views"] == 7


def _story(n=4, titles=True):
    return {"name": "Tale", "title": "Tale", "chapters": n, "word_count": 1000,
            "chapter_titles": ([{"index": i, "title": f"Part {i}"} for i in range(1, n + 1)]
                               if titles else [])}


def _cpub(ch, plat="ao3", reads=10):
    return {"content_type": "story", "story_name": "Tale", "chapter_index": ch, "platform": plat,
            "status": "posted", "account_id": None, "stats": {"views": reads}}


def test_story_chapters_carry_titles_sites_and_reads():
    [w] = _works(stories=[_story()], pubs=[_cpub(1, reads=30), _cpub(1, "sqw", 5), _cpub(2)])
    chs = w["chapters"]
    assert [c["index"] for c in chs] == [1, 2, 3, 4]
    assert chs[0]["title"] == "Part 1"
    assert chs[0]["platforms"] == ["ao3", "sqw"] and chs[0]["stats"]["views"] == 35
    assert chs[2]["status"] == "draft"
    assert w["status"]["label"] == "Ch 1–2 live"
    assert w["chapter_count"] == 4


def test_story_status_names_the_scheduled_range():
    sched = {("story", "Tale"): [
        {"chapter_index": 3, "variant_key": "", "scheduled_at": "2026-10-06 09:00:00"},
        {"chapter_index": 4, "variant_key": "", "scheduled_at": "2026-10-07 09:00:00"}]}
    [w] = _works(stories=[_story()], pubs=[_cpub(1), _cpub(2)], scheduled=sched)
    st = w["status"]
    assert st["label"] == "Ch 1–2 live"
    assert st["scheduled_label"] == "Ch 3–4"
    assert st["next_at"] == "2026-10-06 09:00:00"
    assert w["chapters"][2]["status"] == "scheduled"


def test_a_gap_in_live_chapters_says_how_many():
    [w] = _works(stories=[_story()], pubs=[_cpub(1), _cpub(3)])
    assert w["status"]["label"] == "2 of 4 ch live"


def test_a_one_shot_story_has_no_chapters():
    [w] = _works(stories=[_story(n=1)], pubs=[_cpub(0)])
    assert w["chapters"] == []
    assert w["status"]["label"] == "Live on 1"


def test_untitled_chapters_get_a_number():
    [w] = _works(stories=[_story(n=3, titles=False)])
    assert [c["title"] for c in w["chapters"]] == ["Chapter 1", "Chapter 2", "Chapter 3"]


def test_whole_story_uploads_count_toward_the_story_not_a_chapter():
    [w] = _works(stories=[_story()], pubs=[_cpub(0)])
    assert all(c["status"] == "draft" for c in w["chapters"])
    assert w["status"]["label"] == "Live on 1"


# ── end to end ───────────────────────────────────────────────────────────────

@pytest.fixture
def artwork_archive(tmp_path, monkeypatch):
    from posting import artwork_reader
    arch = tmp_path / "Artwork"
    arch.mkdir()
    monkeypatch.setattr(artwork_reader, "get_artwork_archive_path", lambda: arch)
    return arch


def test_api_works_carries_version_state(artwork_archive, monkeypatch, rows):
    from fastapi.testclient import TestClient
    from posting import artwork_reader
    import dashboard
    monkeypatch.setattr(dashboard.config, "is_dashboard_auth_required", lambda: False)
    name = artwork_reader.create_artwork(title="Two Ways", image_filename="a.png",
                                         image_bytes=b"x", rating="general")
    (artwork_archive / name / "b.png").write_bytes(b"y")
    artwork_reader.save_artwork_metadata(name, {"variants": [
        {"key": "", "label": "Main", "image": "a.png", "rating": ""},
        {"key": "alt", "label": "Alt", "image": "b.png", "rating": "general"}]})
    rows[("ib", "9")] = _ib_row(25, 3, 0)
    conn = get_connection()
    try:
        mq.add_member(conn, name, "ib", "9", variant_key="alt")
        conn.commit()
    finally:
        conn.close()
    works = TestClient(dashboard.app).get("/api/works", params={"type": "artwork"}).json()["works"]
    w = next(x for x in works if x["name"] == name)
    assert w["stats"]["views"] == 25
    assert w["status"]["state"] == "live"
    [v] = w["variants"]
    assert v["key"] == "alt" and v["platforms"] == ["ib"] and v["stats"]["views"] == 25
    assert w["main"]["status"] == "draft"
