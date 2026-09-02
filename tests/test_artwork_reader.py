"""Artwork archive reader tests — create / load / build_artwork_package."""

import json

import pytest

from posting import artwork_reader
from posting.platforms.base import StoryUploadPackage


@pytest.fixture
def artwork_archive(tmp_path, monkeypatch):
    """Point the artwork archive at a temp dir for the duration of a test."""
    arch = tmp_path / "Artwork"
    arch.mkdir()
    monkeypatch.setattr(artwork_reader, "get_artwork_archive_path", lambda: arch)
    return arch


def test_create_and_load_artwork(artwork_archive):
    name = artwork_reader.create_artwork(
        title="Autumn Study",
        image_filename="study.png",
        image_bytes=b"\x89PNG\r\n\x1a\n fake",
        description="A study.",
        rating="mature",
        tags={"default": ["autumn", "study"], "fa": ["autumn", "figure_study"]},
        platforms=["ib", "fa"],
    )
    assert name == "Autumn_Study"

    art = artwork_reader.load_artwork(name)
    assert art.title == "Autumn Study"
    assert art.rating == "mature"
    assert art.image == "study.png"
    # fa's explicit list is retained as-is; ib has no list of its own and is
    # NOT given a cascaded copy at load time (3.17.0 — a present per-platform
    # key means "post exactly these", so cascading disabled the tag budget for
    # every platform). ib inherits when the package is built instead.
    assert art.tags_by_platform["fa"] == ["autumn", "figure_study"]
    assert "ib" not in art.tags_by_platform
    assert artwork_reader.build_artwork_package(art, "ib").tags == [
        "autumn", "study"]
    assert artwork_reader.build_artwork_package(art, "fa").tags == [
        "autumn", "figure_study"]
    assert (artwork_archive / name / "study.png").is_file()
    # Phase 0: new folders are written as masterpiece.json (not the legacy name).
    assert (artwork_archive / name / "masterpiece.json").is_file()
    assert not (artwork_archive / name / "artwork.json").is_file()


def test_build_artwork_package(artwork_archive):
    name = artwork_reader.create_artwork(
        title="Pkg Test", image_filename="a.jpg", image_bytes=b"jpgdata",
        rating="adult",
        tags={"default": ["x"], "ib": ["a", "b"]},
        titles={"fa": "FA Title"},
        descriptions={"default": "desc", "bsky": "announce!"},
        categories={"fa": {"cat": "13", "species": "wolf"}},
    )
    art = artwork_reader.load_artwork(name)

    pkg_ib = artwork_reader.build_artwork_package(art, "ib")
    assert isinstance(pkg_ib, StoryUploadPackage)
    assert pkg_ib.chapter_index == 0
    assert pkg_ib.file_type == "jpg"
    assert pkg_ib.file_path.endswith("a.jpg")
    assert pkg_ib.tags == ["a", "b"]
    assert pkg_ib.title == "Pkg Test"
    assert pkg_ib.rating == "adult"

    pkg_fa = artwork_reader.build_artwork_package(art, "fa")
    assert pkg_fa.title == "FA Title"                 # per-platform title override
    assert pkg_fa.extra == {"cat": "13", "species": "wolf"}
    assert pkg_fa.tags == ["x"]                        # fa cascaded from default

    pkg_bsky = artwork_reader.build_artwork_package(art, "bsky")
    assert pkg_bsky.description == "announce!"         # bsky announcement override


def test_create_dedupes_same_title(artwork_archive):
    n1 = artwork_reader.create_artwork(
        title="Dup", image_filename="a.png", image_bytes=b"1")
    n2 = artwork_reader.create_artwork(
        title="Dup", image_filename="a.png", image_bytes=b"2")
    assert n1 == "Dup"
    assert n2 == "Dup_2"


def test_list_artworks_newest_first(artwork_archive):
    artwork_reader.create_artwork(title="One", image_filename="a.png", image_bytes=b"1")
    artwork_reader.create_artwork(title="Two", image_filename="b.png", image_bytes=b"2")
    items = artwork_reader.list_artworks()
    assert {i["name"] for i in items} == {"One", "Two"}


def test_load_artwork_traversal_guard(artwork_archive):
    with pytest.raises(FileNotFoundError):
        artwork_reader.load_artwork("../../etc/passwd")


# ── Phase 0: masterpiece.json back-compat (legacy artwork.json still works) ──

def _write_legacy(folder, meta):
    folder.mkdir()
    (folder / "img.png").write_bytes(b"x")
    (folder / "artwork.json").write_text(json.dumps(meta), encoding="utf-8")


def test_reads_legacy_artwork_json(artwork_archive):
    # A pre-Phase-0 folder has only artwork.json — still fully readable.
    _write_legacy(artwork_archive / "Legacy_Piece", {
        "title": "Legacy Piece", "rating": "adult", "image": "img.png",
        "tags": {"default": ["old"]}, "platforms": ["fa"],
        "import_source": {"platform": "fa", "submission_id": "123"},
    })
    assert "Legacy_Piece" in {i["name"] for i in artwork_reader.list_artworks()}
    art = artwork_reader.load_artwork("Legacy_Piece")
    assert art.title == "Legacy Piece"
    # `default` is the legacy flat list; it reaches fa through the package
    # builder rather than through a load-time cascade (3.17.0).
    assert art.tags_by_platform["default"] == ["old"]
    assert artwork_reader.build_artwork_package(art, "fa").tags == ["old"]


def test_list_exposes_import_source(artwork_archive):
    # find_existing() relies on import_source coming through list_artworks().
    _write_legacy(artwork_archive / "Sourced", {
        "title": "Sourced", "image": "img.png",
        "import_source": {"platform": "ib", "submission_id": "999"},
    })
    item = next(i for i in artwork_reader.list_artworks() if i["name"] == "Sourced")
    assert item["import_source"] == {"platform": "ib", "submission_id": "999"}


def test_save_migrates_legacy_to_masterpiece(artwork_archive):
    folder = artwork_archive / "Migrate_Me"
    _write_legacy(folder, {"title": "Old", "image": "img.png"})

    artwork_reader.save_artwork_metadata("Migrate_Me", {"title": "New"})
    # Migrate-on-edit: masterpiece.json now exists, legacy artwork.json is gone,
    # and the merged content survives.
    assert (folder / "masterpiece.json").is_file()
    assert not (folder / "artwork.json").is_file()
    assert artwork_reader.load_artwork("Migrate_Me").title == "New"


def test_characters_round_trip(artwork_archive):
    name = artwork_reader.create_artwork(
        title="Char Test", image_filename="a.png", image_bytes=b"1",
        characters=["char_a", "char_b"])
    art = artwork_reader.load_artwork(name)
    assert art.characters == ["char_a", "char_b"]
    listed = next(i for i in artwork_reader.list_artworks() if i["name"] == name)
    assert listed["characters"] == ["char_a", "char_b"]
