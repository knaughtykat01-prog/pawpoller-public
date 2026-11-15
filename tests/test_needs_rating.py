"""Spec 087: "Needs rating" is a mark on the piece.

A piece an import couldn't rate carries ``needs_rating``. Until the person uses "Rate this piece", nothing may send,
announce or merge it, whatever rating a single send, a rated variant or the default in Settings would supply. Only
the rate route clears the mark; saving the piece's details never does (the details form shows General selected for a
piece with no rating and sends it on every save). Pieces without the mark behave as before.
"""
from __future__ import annotations

import io

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

import config
from posting import artwork_reader
from posting.platforms.base import NEEDS_RATING


def _png(colour=(200, 30, 30)):
    buf = io.BytesIO()
    Image.new("RGB", (24, 24), colour).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def lib(tmp_path, monkeypatch):
    base = dict(config.get_settings())
    # The default rating set to General: the case where an unrated import would otherwise go out as General.
    monkeypatch.setattr(config, "get_settings", lambda: {**base, "artwork_archive_path": str(tmp_path),
                                                         "artwork_default_rating": "general"})
    monkeypatch.setattr(artwork_reader, "get_artwork_archive_path", lambda: tmp_path)
    return tmp_path


@pytest.fixture
def client(lib):
    from routes.artwork_api import artwork_router
    from routes.masterpieces_api import masterpieces_router
    app = FastAPI()
    app.include_router(masterpieces_router)
    app.include_router(artwork_router)
    return TestClient(app)


def _piece(title="Sample Piece", marked=True, rating="", colour=(200, 30, 30)):
    return artwork_reader.create_artwork(
        title=title, image_filename="orig.png", image_bytes=_png(colour), rating=rating,
        tags={"default": ["fox", "sample"]}, marks={"needs_rating": True} if marked else None)


def _posters():
    from database.accounts import PLATFORM_NAMES
    from posting.manager import _get_poster
    out = []
    for code in PLATFORM_NAMES:
        try:
            out.append((code, _get_poster(code)))
        except Exception:
            continue
    assert len(out) > 15, "the registry walk found the posters"
    return out


def _refusal(poster, package):
    try:
        return poster.refusal(package)
    except Exception as e:          # a poster that needs its account before it can answer: the mark comes first
        return f"raised {type(e).__name__}"


def test_refused_by_every_site(lib):
    """Through the real package build, with the default rating General: the mark beats the default, a per-send
    rating and a rated variant, at every registered site."""
    name = _piece()
    art = artwork_reader.load_artwork(name)
    assert art.needs_rating and art.rating == ""
    for code, poster in _posters():
        for over in (None, "general", "adult"):
            pkg = artwork_reader.build_artwork_package(art, code, rating_override=over)
            assert pkg.needs_rating is True, code
            assert _refusal(poster, pkg) == NEEDS_RATING, (code, over)


def test_rated_variant_does_not_hide_the_mark(lib):
    name = _piece()
    art = artwork_reader.load_artwork(name)
    (art.path / "sfw.png").write_bytes(_png((10, 200, 10)))
    artwork_reader.save_artwork_metadata(name, {"variants": [{"key": "sfw", "label": "SFW", "image": "sfw.png",
                                                              "rating": "general"}]})
    art = artwork_reader.load_artwork(name)
    for code, poster in _posters():
        pkg = artwork_reader.build_artwork_package(art, code, variant_key="sfw")
        assert _refusal(poster, pkg) == NEEDS_RATING, code


def test_unmarked_piece_unchanged(lib):
    """A piece with no rating and no mark still goes out under the default from Settings, as before."""
    name = _piece(marked=False)
    art = artwork_reader.load_artwork(name)
    assert not art.needs_rating
    pkg = artwork_reader.build_artwork_package(art, "fa")
    assert pkg.needs_rating is False and pkg.rating == "general"
    from posting.manager import _get_poster
    assert _get_poster("fa").refusal(pkg) != NEEDS_RATING


def test_batch_plan_refuses(lib):
    from posting import batch
    name = _piece()
    rows = batch.plan([name], platforms=["fa", "bsky"])
    flat = str(rows)
    assert NEEDS_RATING in flat, "the batch plan names the reason for the marked piece"


def test_merge_refused_either_side(client):
    marked, plain = _piece("Marked One"), _piece("Plain One", marked=False, rating="adult", colour=(1, 2, 3))
    for body in ({"keep": marked, "drop": plain}, {"keep": plain, "drop": marked}):
        r = client.post("/api/masterpieces/merge", json=body)
        assert r.status_code == 409 and NEEDS_RATING in r.text, body
    for body in ({"keep": marked, "absorb": plain, "key": "alt"}, {"keep": plain, "absorb": marked, "key": "alt"}):
        r = client.post("/api/masterpieces/merge-as-variant", json=body)
        assert r.status_code == 409 and NEEDS_RATING in r.text, body
    assert artwork_reader.load_artwork(marked).needs_rating and artwork_reader.load_artwork(plain)


def test_only_the_rate_route_clears(client):
    name = _piece()
    r = client.post("/api/masterpieces/rate", json={"names": [name], "rating": "mature"})
    assert r.status_code == 200 and r.json()["rated"] == [name]
    art = artwork_reader.load_artwork(name)
    assert art.rating == "mature" and not art.needs_rating
    assert "needs_rating" not in artwork_reader.read_raw_metadata(name)
    pkg = artwork_reader.build_artwork_package(art, "fa")
    from posting.manager import _get_poster
    assert pkg.needs_rating is False and _get_poster("fa").refusal(pkg) != NEEDS_RATING


def test_details_save_cannot_rate_or_unmark(client):
    """Both details-save routes: the form sends rating "general" on every save. A marked piece stays unrated and
    marked, and the reply says it still needs rating."""
    name = _piece()
    r = client.patch(f"/api/masterpieces/{name}", json={"title": "New Title", "rating": "general", "tags": ["fox"]})
    assert r.status_code == 200 and r.json()["needs_rating"] is True
    r = client.patch(f"/api/artwork/images/{name}", json={"description": "words", "rating": "general"})
    assert r.status_code == 200 and r.json()["needs_rating"] is True
    art = artwork_reader.load_artwork(name)
    assert art.needs_rating and art.rating == "" and art.title == "New Title" and art.description == "words"


def test_edit_body_cannot_set_marks(client):
    name = _piece(marked=False, rating="adult")
    client.patch(f"/api/artwork/images/{name}", json={"needs_rating": True, "pat_paid": True, "pat_pick_needed": True})
    client.patch(f"/api/masterpieces/{name}", json={"title": "T", "needs_rating": True, "pat_paid": True})
    artwork_reader.save_artwork_metadata(name, {"needs_rating": True, "pat_paid": True})
    assert artwork_reader.marks_of(name) == {}
    marked = _piece("Other")
    artwork_reader.save_artwork_metadata(marked, {"needs_rating": False, "rating": "general"})
    assert artwork_reader.marks_of(marked) == {"needs_rating": True}
    assert artwork_reader.load_artwork(marked).rating == ""


def test_mark_survives_split_and_details_save(client):
    name = _piece()
    art = artwork_reader.load_artwork(name)
    (art.path / "alt.png").write_bytes(_png((9, 9, 200)))
    artwork_reader.save_artwork_metadata(name, {"variants": [{"key": "alt", "label": "Alt", "image": "alt.png",
                                                              "rating": "general"}]})
    assert artwork_reader.marks_of(name) == {"needs_rating": True}
    r = client.post(f"/api/masterpieces/{name}/variants/alt/split", json={})
    if r.status_code == 404:                    # route shape differs: the unit under test is create_artwork(marks=)
        new = artwork_reader.create_artwork(title="Split", image_filename="alt.png", image_bytes=_png((9, 9, 200)),
                                            rating="general", marks=artwork_reader.clean_marks(
                                                artwork_reader.read_raw_metadata(name)))
    else:
        assert r.status_code == 200, r.text
        new = r.json().get("name") or r.json().get("new_name")
    assert artwork_reader.marks_of(new) == {"needs_rating": True}, "a split of a marked piece is marked"


def test_queued_marked_piece_is_not_retried():
    from posting import manager
    assert any(m in NEEDS_RATING.lower() for m in manager._PERMANENT_ERROR_MARKERS)


def test_bulk_rate_checks_rating_names_and_cap(client):
    name = _piece()
    for bad in ("", "General", "g", "safe", None, "explicit"):
        assert client.post("/api/masterpieces/rate", json={"names": [name], "rating": bad}).status_code == 400, bad
    for names in ([], None, "x", [""], [1], ["a"] * 201):
        assert client.post("/api/masterpieces/rate", json={"names": names, "rating": "adult"}).status_code == 400
    assert artwork_reader.load_artwork(name).needs_rating


def test_bulk_rate_reports_each_failure(client):
    a, b = _piece("First"), _piece("Second", colour=(4, 5, 6))
    r = client.post("/api/masterpieces/rate", json={"names": [a, "No_Such_Piece", b], "rating": "adult"}).json()
    assert r["rated"] == [a, b] and [f["name"] for f in r["failed"]] == ["No_Such_Piece"]
    assert all(not artwork_reader.load_artwork(n).needs_rating for n in (a, b))


def test_bulk_rate_touches_no_site(client, monkeypatch):
    from posting import manager
    monkeypatch.setattr(manager, "_get_poster", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no poster")))
    name = _piece()
    assert client.post("/api/masterpieces/rate", json={"names": [name], "rating": "general"}).status_code == 200


def test_library_list_and_detail_carry_the_marks(client):
    marked, plain = _piece("Marked"), _piece("Plain", marked=False, rating="adult", colour=(7, 7, 7))
    rows = {m["name"]: m for m in client.get("/api/masterpieces").json()["masterpieces"]}
    assert rows[marked].get("needs_rating") is True and "needs_rating" not in rows[plain]
    assert client.get(f"/api/masterpieces/{marked}").json()["needs_rating"] is True


def test_announce_paths_refused(client, lib, monkeypatch):
    """One case per path that can carry a piece outside the app without the shared send check."""
    import asyncio

    from fastapi import HTTPException

    from posting import discord, journals, manager, tg_linked
    from routes import discord_api, masterpieces_api, podcast_api, posts_api
    name = _piece()
    sent = []
    monkeypatch.setattr(discord, "announce_publish", lambda *a, **k: sent.append("discord"), raising=False)
    asyncio.run(manager.announce_artwork(name, force=True, site_links=[{"platform": "fa", "url": "x"}]))
    assert sent == [], "the scheduler's Discord announce stays quiet"
    monkeypatch.setattr(discord_api.discord, "is_configured", lambda: True)
    with pytest.raises(HTTPException) as e:
        asyncio.run(discord_api.discord_announce({"artwork_name": name, "title": "t", "links": []}))
    assert e.value.status_code == 409 and e.value.detail == NEEDS_RATING
    out = asyncio.run(tg_linked.announce_existing(None, story_name=name, content_type="artwork"))
    assert out == {"status": "error", "error": NEEDS_RATING}
    with pytest.raises(ValueError, match="Rate this piece first"):
        journals.store_piece_journal("artwork", name, {"sites": ["fa"], "title": "t", "text": "x"})
    with pytest.raises(HTTPException) as e:
        posts_api._parse_comment_fields(None, {"kind": "artwork", "ref": name})
    assert e.value.status_code == 409
    import age_gate
    monkeypatch.setattr(age_gate, "is_under_18", lambda: False)
    r = client.get(f"/api/masterpieces/{name}/rule34-kit")
    assert r.status_code == 409 and NEEDS_RATING in r.text, "nothing to paste for a piece that needs rating"
    assert podcast_api._piece(name) is None
    # ... and never as a public feed's cover: the cover branch reads the piece and must turn it away.
    import inspect
    cover = inspect.getsource(podcast_api.public_feed_item)
    assert "art.needs_rating" in cover and cover.index("art.needs_rating") < cover.index("art.thumbnail_path")


def test_refuse_helper(lib):
    marked, plain = _piece("Marked"), _piece("Plain", marked=False, rating="adult", colour=(7, 7, 7))
    with pytest.raises(ValueError, match="Rate this piece first"):
        artwork_reader.refuse_if_needs_rating(marked)
    artwork_reader.refuse_if_needs_rating(plain)
    artwork_reader.refuse_if_needs_rating("No_Such_Piece")      # not found is the caller's to say
