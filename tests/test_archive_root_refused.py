"""SEC070 (3): a name that resolves to the archive itself ("." or "") is not a piece or a story.

It passes the inside-the-archive check, so the delete routes would remove the whole archive.
"""
import asyncio

import pytest
from fastapi import HTTPException

from posting import artwork_reader
from routes import artwork_api, editor_api


@pytest.mark.parametrize("name", [".", "", "./", "Piece/.."])
def test_artwork_delete_refuses_the_archive(tmp_path, monkeypatch, name):
    (tmp_path / "artwork.json").write_text("{}", encoding="utf-8")     # even with a meta file at the root
    (tmp_path / "Piece").mkdir()
    monkeypatch.setattr(artwork_reader, "get_artwork_archive_path", lambda: tmp_path)
    with pytest.raises(HTTPException) as e:
        artwork_api.delete_artwork(name)
    assert e.value.status_code == 404
    assert (tmp_path / "Piece").is_dir()


@pytest.mark.parametrize("name", [".", "Sample_Story/.."])
def test_story_delete_refuses_the_archive(tmp_path, monkeypatch, name):
    (tmp_path / "Sample_Story").mkdir()
    monkeypatch.setattr(editor_api, "get_archive_path", lambda: tmp_path)
    with pytest.raises(HTTPException) as e:
        asyncio.run(editor_api.delete_story(name, confirm_name=tmp_path.name))
    assert e.value.status_code == 400
    assert (tmp_path / "Sample_Story").is_dir()
