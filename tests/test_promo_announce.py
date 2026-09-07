"""A story's promo card as its announcement image (Promo Maker v2 release 3, 4.17.0; spec §4).

story.json ``images.promo`` names a saved promo. The announcers (Telegram / X / Bluesky) read
``package.thumbnail_path`` for a story, so ``build_package`` hands them the card's PNG for those
platforms only — a hosting site's story listing keeps the cover. ``/api/promos/{id}/announce``
writes the choice the way the editor writes story.json (backup + atomic replace).
"""
from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import config
from posting import story_reader
from routes import promos_api

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def _archive(tmp_path, promo_id=7):
    story = tmp_path / "archive" / "Sample_Story"
    (story / "Markdown").mkdir(parents=True)
    (story / "Markdown" / "MASTER.md").write_text("# Sample Story\n\nOnce.\n", encoding="utf-8")
    (story / "cover.png").write_bytes(PNG)
    sj = {"title": "Sample Story", "author": "owner", "total_chapters": 1, "total_words": 10,
          "description": "A sample.", "chapter_info": [], "images": {"cover": "cover.png"}}
    if promo_id is not None:
        sj["images"]["promo"] = promo_id
    (story / "story.json").write_text(json.dumps(sj), encoding="utf-8")
    return tmp_path / "archive"


@pytest.fixture
def archive(tmp_path, monkeypatch):
    root = _archive(tmp_path)
    monkeypatch.setattr(story_reader, "get_archive_path", lambda: root)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    (tmp_path / "data" / "promos").mkdir(parents=True)
    return root


def test_announcers_get_the_promo_card_and_hosting_sites_keep_the_cover(archive, tmp_path):
    promo_png = tmp_path / "data" / "promos" / "7.png"
    promo_png.write_bytes(PNG)
    story = story_reader.load_story("Sample_Story")
    assert story.announcement_image == str(promo_png)
    assert story.thumbnail_path.endswith("cover.png")
    for plat in ("tg", "tw", "bsky"):
        assert story_reader.build_package(story, 0, plat).thumbnail_path == str(promo_png), plat
    for plat in ("fa", "sf", "ib"):
        assert story_reader.build_package(story, 0, plat).thumbnail_path.endswith("cover.png"), plat


def test_a_missing_or_bad_promo_falls_back_to_the_cover(archive, tmp_path):
    story = story_reader.load_story("Sample_Story")               # images.promo = 7 but no 7.png
    assert story.announcement_image is None
    assert story_reader.build_package(story, 0, "tg").thumbnail_path.endswith("cover.png")
    assert story_reader._promo_image("seven") is None and story_reader._promo_image(None) is None


@pytest.fixture
def api(archive, tmp_path):
    app = FastAPI()
    app.include_router(promos_api.promos_router)
    return TestClient(app)


def _spec():
    return json.dumps({"version": 2, "text": "Once upon a time.", "highlights": [], "styles": [],
                       "size": {"preset": "custom", "w": 1080, "h": 1080}, "bg": "blush",
                       "font": {"family": "lora", "px": 60}, "footer": "", "story": "Sample_Story"})


def test_announce_round_trip_writes_story_json_with_a_backup(api, archive, tmp_path):
    sj = archive / "Sample_Story" / "story.json"
    r = api.post("/api/promos", data={"spec": _spec(), "title": "Teaser", "story_name": "Sample_Story"},
                 files={"png": ("c.png", PNG, "image/png")})
    assert r.status_code == 201, r.text
    pid = r.json()["promo_id"]
    assert r.json()["pages"] == 1
    # the archive fixture pre-set images.promo = 7 (a card that does not exist): not "announce"
    assert api.get("/api/promos", params={"story": "Sample_Story"}).json()["promos"][0]["announce"] is False

    r = api.post(f"/api/promos/{pid}/announce")
    assert r.status_code == 200 and r.json()["announce"] is True
    assert json.loads(sj.read_text(encoding="utf-8"))["images"]["promo"] == pid
    assert list(sj.parent.glob("story.json.bak.*")), "the editor-style backup was written"
    assert api.get("/api/promos", params={"story": "Sample_Story"}).json()["promos"][0]["announce"] is True

    # the loader now hands the announcers this card
    story = story_reader.load_story("Sample_Story")
    assert story_reader.build_package(story, 0, "tg").thumbnail_path == str(tmp_path / "data" / "promos" / f"{pid}.png")

    r = api.delete(f"/api/promos/{pid}/announce")
    assert r.status_code == 200 and r.json()["announce"] is False
    assert "promo" not in json.loads(sj.read_text(encoding="utf-8"))["images"]
    assert json.loads(sj.read_text(encoding="utf-8"))["images"]["cover"] == "cover.png"      # untouched


def test_deleting_the_announced_card_clears_the_choice(api, archive):
    sj = archive / "Sample_Story" / "story.json"
    pid = api.post("/api/promos", data={"spec": _spec(), "story_name": "Sample_Story"},
                   files={"png": ("c.png", PNG, "image/png")}).json()["promo_id"]
    api.post(f"/api/promos/{pid}/announce")
    assert json.loads(sj.read_text(encoding="utf-8"))["images"]["promo"] == pid
    assert api.delete(f"/api/promos/{pid}").status_code == 204
    assert "promo" not in json.loads(sj.read_text(encoding="utf-8"))["images"]


def test_a_free_standing_card_cannot_announce(api):
    pid = api.post("/api/promos", data={"spec": _spec()}, files={"png": ("c.png", PNG, "image/png")}).json()["promo_id"]
    r = api.post(f"/api/promos/{pid}/announce")
    assert r.status_code == 400 and "not attached" in r.json()["detail"]
    assert api.post("/api/promos/999/announce").status_code == 404
