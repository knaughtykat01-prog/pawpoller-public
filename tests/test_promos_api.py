"""Saved promo cards — /api/promos (Promo Maker v2 release 2, 4.16.0; spec §3.5).

The card is stored as its spec + PNG (+ photo background) under DATA_DIR/promos and
addressed only by the row's stored file names. These tests run the real router against
the per-test database (conftest's isolated DB) with the promos folder pointed at tmp_path.
"""
from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routes import promos_api

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def _spec(**over):
    s = {"version": 2, "text": "She smirks, and something in the room shifts.", "highlights": [{"start": 0, "end": 10, "color": "#f7b6d2"}],
         "styles": [{"start": 4, "end": 10, "b": True}], "size": {"preset": "custom", "w": 1200, "h": 900},
         "bg": "blush", "font": {"family": "lora", "px": 60}, "footer": "@SecondFur", "story": None}
    s.update(over)
    return json.dumps(s)


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setattr(promos_api, "promos_dir", lambda: (tmp_path / "promos").mkdir(exist_ok=True) or (tmp_path / "promos"))
    monkeypatch.setattr(promos_api, "_story_exists", lambda name: name == "Sample Story")
    app = FastAPI()
    app.include_router(promos_api.promos_router)
    return TestClient(app)


def test_create_list_get_update_delete_round_trip(api, tmp_path):
    r = api.post("/api/promos", data={"spec": _spec(), "title": "Teaser", "story_name": "Sample Story"},
                 files={"png": ("card.png", PNG, "image/png")})
    assert r.status_code == 201, r.text
    row = r.json()
    pid = row["promo_id"]
    assert row["title"] == "Teaser" and row["story_name"] == "Sample Story"
    assert row["width"] == 1200 and row["height"] == 900 and row["has_background"] is False
    assert row["image_url"] == f"/api/promos/{pid}/image"
    assert (tmp_path / "promos" / f"{pid}.png").read_bytes() == PNG

    assert [p["promo_id"] for p in api.get("/api/promos").json()["promos"]] == [pid]
    assert api.get("/api/promos", params={"story": "Sample Story"}).json()["promos"][0]["promo_id"] == pid
    assert api.get("/api/promos", params={"story": "Other"}).json()["promos"] == []

    got = api.get(f"/api/promos/{pid}").json()
    assert got["spec"]["text"].startswith("She smirks") and got["spec"]["styles"][0]["b"] is True

    img = api.get(f"/api/promos/{pid}/image")
    assert img.status_code == 200 and img.content == PNG and img.headers["cache-control"] == "no-store"
    assert api.get(f"/api/promos/{pid}/background").status_code == 404

    # update: new spec + png + a photo background
    png2 = PNG + b"v2"
    r = api.put(f"/api/promos/{pid}", data={"spec": _spec(text="New words here."), "title": "Teaser 2"},
                files={"png": ("card.png", png2, "image/png"), "background": ("photo.jpg", b"\xff\xd8jpeg", "image/jpeg")})
    assert r.status_code == 200, r.text
    assert r.json()["title"] == "Teaser 2" and r.json()["has_background"] is True
    assert api.get(f"/api/promos/{pid}/image").content == png2
    assert api.get(f"/api/promos/{pid}/background").content == b"\xff\xd8jpeg"
    assert (tmp_path / "promos" / f"{pid}-bg.jpg").is_file()
    assert api.get(f"/api/promos/{pid}").json()["spec"]["text"] == "New words here."

    # clearing the background removes the file
    r = api.put(f"/api/promos/{pid}", data={"spec": _spec(), "clear_background": "1"},
                files={"png": ("card.png", PNG, "image/png")})
    assert r.status_code == 200 and r.json()["has_background"] is False
    assert not (tmp_path / "promos" / f"{pid}-bg.jpg").exists()

    # delete removes the row and the files
    assert api.delete(f"/api/promos/{pid}").status_code == 204
    assert api.get(f"/api/promos/{pid}").status_code == 404
    assert api.get(f"/api/promos/{pid}/image").status_code == 404
    assert not (tmp_path / "promos" / f"{pid}.png").exists()
    assert api.delete(f"/api/promos/{pid}").status_code == 404


def test_listing_is_newest_first(api):
    ids = []
    for i in range(3):
        ids.append(api.post("/api/promos", data={"spec": _spec(), "title": f"c{i}"},
                            files={"png": ("c.png", PNG, "image/png")}).json()["promo_id"])
    assert [p["promo_id"] for p in api.get("/api/promos").json()["promos"]] == ids[::-1]


@pytest.mark.parametrize("bad, msg", [
    ("not json", "JSON"),
    (json.dumps({"version": 1, "text": "x", "size": {"w": 1080, "h": 1080}}), "version-2"),
    (_spec(text="   "), "text is required"),
    (_spec(text="x" * 20_001), "longer"),
    (_spec(size={"w": 100, "h": 1080}), "320"),
    (_spec(highlights=[{"start": 5, "end": 999, "color": "#a"}]), "outside the text"),
    (_spec(styles=[{"start": 3, "end": 3, "b": True}]), "outside the text"),
])
def test_spec_validation(api, bad, msg):
    r = api.post("/api/promos", data={"spec": bad}, files={"png": ("c.png", PNG, "image/png")})
    assert r.status_code == 400, r.text
    assert msg in r.json()["detail"]


def test_png_must_be_a_png_and_within_the_limit(api, monkeypatch):
    r = api.post("/api/promos", data={"spec": _spec()}, files={"png": ("c.png", b"GIF89a", "image/png")})
    assert r.status_code == 415
    monkeypatch.setattr(promos_api, "MAX_PNG_BYTES", 32)
    r = api.post("/api/promos", data={"spec": _spec()}, files={"png": ("c.png", PNG, "image/png")})
    assert r.status_code == 413


def test_background_must_be_an_image(api):
    r = api.post("/api/promos", data={"spec": _spec()},
                 files={"png": ("c.png", PNG, "image/png"), "background": ("x.txt", b"hello", "text/plain")})
    assert r.status_code == 415


def test_story_name_must_be_a_real_story(api):
    r = api.post("/api/promos", data={"spec": _spec(), "story_name": "Nope"}, files={"png": ("c.png", PNG, "image/png")})
    assert r.status_code == 400 and "story" in r.json()["detail"]
    # an empty story_name is "free-standing", not an error
    r = api.post("/api/promos", data={"spec": _spec(), "story_name": ""}, files={"png": ("c.png", PNG, "image/png")})
    assert r.status_code == 201 and r.json()["story_name"] is None


def test_stored_file_names_cannot_escape_the_folder(api, tmp_path):
    """A row whose file name points outside DATA_DIR/promos serves nothing."""
    from database import promos as pdb
    from database.db import get_connection
    pid = api.post("/api/promos", data={"spec": _spec()}, files={"png": ("c.png", PNG, "image/png")}).json()["promo_id"]
    outside = tmp_path / "secret.png"
    outside.write_bytes(PNG)
    conn = get_connection()
    try:
        conn.execute("UPDATE promos SET png_file = ? WHERE promo_id = ?", ("../secret.png", pid))
        conn.commit()
    finally:
        conn.close()
    assert api.get(f"/api/promos/{pid}/image").status_code == 404
    assert outside.exists()                                   # and delete never reaches it
    api.delete(f"/api/promos/{pid}")
    assert outside.exists()
    assert pdb.get_promo.__name__ == "get_promo"


def test_story_exists_checks_the_archive(tmp_path, monkeypatch):
    from posting import story_reader
    (tmp_path / "Sample Story" / "Markdown").mkdir(parents=True)
    (tmp_path / "Sample Story" / "Markdown" / "MASTER.md").write_text("# x", encoding="utf-8")
    (tmp_path / "Empty").mkdir()
    monkeypatch.setattr(story_reader, "get_archive_path", lambda: tmp_path)
    assert promos_api._story_exists("Sample Story") is True
    assert promos_api._story_exists("Empty") is False
    assert promos_api._story_exists("../Sample Story") is False
    assert promos_api._story_exists("Missing") is False
