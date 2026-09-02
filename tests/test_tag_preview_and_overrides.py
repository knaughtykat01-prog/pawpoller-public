"""Per-platform tag previews and overrides (3.12.0).

The canonical set is meant to be rich; `core` is a priority ORDER, and each
platform trims from the tail to its own budget. Two consequences the UI has to
be able to show:

  * a budget quietly eating 40 tags on DeviantArt looks identical, from the
    Masterpiece page, to a work that was only tagged twice;
  * where the automatic tail-trim picks the wrong survivors, you want to curate
    that platform's list by hand — an override — and an override is posted
    VERBATIM, so it must be visibly different from a trim.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from posting import artwork_reader


@pytest.fixture()
def client():
    from dashboard import app
    return TestClient(app)


@pytest.fixture()
def work(tmp_path, monkeypatch):
    root = tmp_path / "artwork"
    folder = root / "BudgetTarget"
    folder.mkdir(parents=True)
    (folder / "image.jpg").write_bytes(b"\xff\xd8\xff\xd9")
    (folder / "masterpiece.json").write_text(json.dumps({
        "title": "Budget Target", "rating": "general", "image": "image.jpg",
        "tags": {"core": [f"core{i}" for i in range(10)],
                 "auxiliary": [f"aux{i}" for i in range(40)]},
    }), encoding="utf-8")
    monkeypatch.setattr(artwork_reader, "get_artwork_archive_path", lambda: root)
    return "BudgetTarget"


def _rows(client, work):
    r = client.get(f"/api/masterpieces/{work}/tag-preview")
    assert r.status_code == 200, r.text
    return {p["platform"]: p for p in r.json()["platforms"]}


def test_the_preview_shows_what_each_platform_actually_gets(client, work):
    rows = _rows(client, work)
    assert rows["da"]["sent"] == 30 and rows["da"]["total"] == 50
    assert len(rows["da"]["dropped"]) == 20
    assert rows["ib"]["sent"] == 50 and rows["ib"]["dropped"] == []
    assert rows["sf"]["sent"] == 50, "50 is under SoFurry's 97"


def test_the_limit_is_stated_in_words(client, work):
    rows = _rows(client, work)
    assert rows["da"]["limit"] == "30 tags max"
    assert rows["fa"]["limit"] == "500 characters max"
    assert rows["ib"]["limit"] == "no limit"


def test_the_core_set_survives_every_trim(client, work):
    """Core-first ordering is the whole mechanism: whatever a platform keeps has
    to start with the tags declared most important."""
    r = client.get(f"/api/masterpieces/{work}/tag-preview").json()
    assert r["core_count"] == 10
    dropped = {p["platform"]: set(p["dropped"]) for p in r["platforms"]}
    for platform, gone in dropped.items():
        assert not any(t.startswith("core") for t in gone), platform


def test_an_override_can_be_set_and_is_reported_as_one(client, work):
    r = client.patch(f"/api/masterpieces/{work}",
                     json={"platform_tags": {"fa": ["hand", "picked", "few"]}})
    assert r.status_code == 200, r.text
    rows = _rows(client, work)
    assert rows["fa"]["override"] is True
    assert rows["fa"]["sent"] == 3
    assert rows["fa"]["dropped"] == [], "an override is posted verbatim, nothing is trimmed"
    # Everyone else still takes the canonical set.
    assert rows["ib"]["override"] is False and rows["ib"]["sent"] == 50


def test_an_override_actually_reaches_the_package(client, work):
    """The preview would be a lie if the posting path ignored it."""
    client.patch(f"/api/masterpieces/{work}",
                 json={"platform_tags": {"fa": ["only", "these", "three"]}})
    art = artwork_reader.load_artwork(work)
    pkg = artwork_reader.build_artwork_package(art, "fa")
    assert pkg.tags == ["only", "these", "three"]


def test_clearing_an_override_returns_to_the_automatic_trim(client, work):
    client.patch(f"/api/masterpieces/{work}", json={"platform_tags": {"fa": ["a", "b"]}})
    assert _rows(client, work)["fa"]["override"] is True
    client.patch(f"/api/masterpieces/{work}", json={"platform_tags": {"fa": None}})
    rows = _rows(client, work)
    assert rows["fa"]["override"] is False
    assert rows["fa"]["sent"] == 50


def test_an_override_does_not_disturb_the_canonical_set(client, work):
    client.patch(f"/api/masterpieces/{work}", json={"platform_tags": {"da": ["x"]}})
    raw = artwork_reader.read_raw_metadata(work)
    assert len(raw["tags"]["core"]) == 10
    assert len(raw["tags"]["auxiliary"]) == 40
    assert raw["tags"]["da"] == ["x"]


def test_a_canonical_edit_does_not_wipe_an_override(client, work):
    """Saving canonical tags rewrites core/auxiliary. A real per-platform
    override is deliberate and must survive that."""
    client.patch(f"/api/masterpieces/{work}", json={"platform_tags": {"da": ["kept"]}})
    client.patch(f"/api/masterpieces/{work}", json={"tags": ["core0", "core1", "newtag"]})
    raw = artwork_reader.read_raw_metadata(work)
    assert raw["tags"]["da"] == ["kept"]


def test_a_reserved_key_cannot_be_written_as_a_platform(client, work):
    """'core' is not a platform; letting it through here would silently destroy
    the priority ordering."""
    for key in ("core", "auxiliary", "default"):
        r = client.patch(f"/api/masterpieces/{work}", json={"platform_tags": {key: ["x"]}})
        assert r.status_code == 400, key


def test_a_non_list_override_is_rejected(client, work):
    r = client.patch(f"/api/masterpieces/{work}", json={"platform_tags": {"fa": "not-a-list"}})
    assert r.status_code == 400


def test_preview_on_a_missing_work_is_404(client, work):
    assert client.get("/api/masterpieces/NoSuchWork/tag-preview").status_code == 404
