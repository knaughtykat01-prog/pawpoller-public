"""Stored file names stay inside their own folder (4.43.1, IMGCONFINE).

A piece's image / thumbnail and a story's cover / chapter thumbnails are strings in a
json file. ``../OtherPiece/adult.png`` used to resolve and be posted (or sent to Discord)
as if it were this piece's picture. Renders always had this guard; now everything does.
"""
from __future__ import annotations

import json
from pathlib import Path

from posting import artwork_reader, story_reader
from posting.manager import _discord_preview_source


def test_inside():
    root = Path("C:/lib/Piece") if Path("C:/").exists() else Path("/lib/Piece")
    assert artwork_reader.inside(root, "a.png") == (root / "a.png").resolve()
    assert artwork_reader.inside(root, "sub/b.png") == (root / "sub/b.png").resolve()
    assert artwork_reader.inside(root, "../Other/adult.png") is None
    assert artwork_reader.inside(root, "") is None


def test_a_piece_image_outside_its_folder_is_never_used(tmp_path, monkeypatch):
    arch = tmp_path / "Artwork"
    arch.mkdir()
    monkeypatch.setattr(artwork_reader, "get_artwork_archive_path", lambda: arch)
    other = artwork_reader.create_artwork(title="Other", image_filename="adult.png", image_bytes=b"x")
    mine = artwork_reader.create_artwork(title="Mine", image_filename="a.png", image_bytes=b"y")
    artwork_reader.save_artwork_metadata(mine, {"image": f"../{other}/adult.png",
                                                "thumbnail": f"../{other}/adult.png"})
    art = artwork_reader.load_artwork(mine)
    assert art.image_path is None and art.thumbnail_path is None
    assert _discord_preview_source(art) is None
    ok = artwork_reader.load_artwork(other)
    assert ok.image_path and Path(ok.image_path).name == "adult.png"


def test_a_story_cover_outside_its_folder_is_ignored(tmp_path):
    story = tmp_path / "Some_Story"
    (story / "Markdown").mkdir(parents=True)
    (story / "cover.png").write_bytes(b"c")
    (tmp_path / "elsewhere.png").write_bytes(b"e")
    data = {"title": "Some Story", "images": {"cover": "../elsewhere.png",
                                              "chapter_thumbnails": {"1": "cover.png", "2": "../elsewhere.png"}}}
    (story / "story.json").write_text(json.dumps(data), encoding="utf-8")
    info = story_reader._load_from_story_json("Some_Story", story, story / "story.json")
    assert info.thumbnail_path is None
    assert list(info.chapter_thumbnails) == [1]
