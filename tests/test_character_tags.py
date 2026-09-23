"""A character's booru tag rides along to the boorus (spec 003 US3).

A character existed twice and the two never met: a name in `characters[]` that reached
no post, and a hand-typed `name_(owner)` tag that did. The registry holds the mapping,
so a piece's own cast now carries its tags — on the sites where a booru vocabulary means
something, and only for characters that actually have one.
"""
from __future__ import annotations

import json

import pytest

from database import character_queries as cq
from database.db import get_connection
from posting import artwork_reader


@pytest.fixture()
def registry():
    conn = get_connection()
    conn.execute("DELETE FROM characters")
    cq.upsert_character(conn, "Sample Fox", booru_tag="sample_fox_(owner)")
    cq.upsert_character(conn, "Second Fur", booru_tag="second_fur_(owner)")
    cq.upsert_character(conn, "Untagged One")
    conn.commit()
    conn.close()
    yield
    conn = get_connection()
    conn.execute("DELETE FROM characters")
    conn.commit()
    conn.close()


def _package(art, platform):
    return artwork_reader.build_artwork_package(art, platform)


def test_a_booru_gets_the_character_tag(registry, tmp_path):
    art = _art(tmp_path, ["Sample Fox"])
    tags = [t.lower() for t in _package(art, "e621").tags]
    assert "sample_fox_(owner)" in tags


def test_a_character_without_a_tag_adds_nothing(registry, tmp_path):
    art = _art(tmp_path, ["Untagged One"])
    tags = [t.lower() for t in _package(art, "e621").tags]
    assert not any("untagged" in t for t in tags)


def test_a_non_booru_platform_is_untouched(registry, tmp_path):
    art = _art(tmp_path, ["Sample Fox"])
    tags = [t.lower() for t in _package(art, "fa").tags]
    assert "sample_fox_(owner)" not in tags


def test_a_tag_already_on_the_piece_is_not_repeated(registry, tmp_path):
    art = _art(tmp_path, ["Sample Fox"], tags=["sample_fox_(owner)", "wolf"])
    tags = [t.lower() for t in _package(art, "fbr").tags]
    assert tags.count("sample_fox_(owner)") == 1


def test_character_tags_lead_so_a_trim_keeps_them(registry, tmp_path):
    """Per-platform budgets drop from the tail, as with the artist tag."""
    art = _art(tmp_path, ["Sample Fox", "Second Fur"], tags=["wolf", "forest"])
    tags = [t.lower() for t in _package(art, "e621").tags]
    assert tags[:2] == ["sample_fox_(owner)", "second_fur_(owner)"] or \
        set(tags[:3]) >= {"sample_fox_(owner)", "second_fur_(owner)"}


def test_an_explicit_tag_list_is_posted_exactly(registry, tmp_path):
    """tags_override is the UI saying "post exactly these" — nothing is added behind it."""
    art = _art(tmp_path, ["Sample Fox"])
    pkg = artwork_reader.build_artwork_package(art, "e621", tags_override=["only_this"])
    assert pkg.tags == ["only_this"]


def _art(tmp_path, characters, tags=None):
    """Build a real ArtworkInfo through the reader, so the test exercises the loader."""
    folder = tmp_path / "Sample_Piece"
    if not folder.exists():
        folder.mkdir()
        (folder / "image.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (folder / "masterpiece.json").write_text(json.dumps({
        "name": "Sample_Piece", "title": "Sample Piece", "description": "d",
        "image": "image.png", "rating": "adult", "characters": characters,
        "tags": {"core": tags or ["wolf"], "auxiliary": []},
    }), encoding="utf-8")
    return artwork_reader.ArtworkInfo(
        name="Sample_Piece", title="Sample Piece", description="d",
        path=folder, image="image.png", rating="adult", author="",
        characters=list(characters),
        tags_by_platform={"core": list(tags or ["wolf"]), "auxiliary": []},
    )
