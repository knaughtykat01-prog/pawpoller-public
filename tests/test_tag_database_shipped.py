"""The editor's tag picker needs its files inside the build (backlog TAGDBSHIP).

`routes/editor_api.py::_TAG_DB_DIR` resolves `tag_database/` next to the code, which in a
frozen install is `…/_internal/tag_database`. Neither .spec listed the folder, so every
packaged desktop and server build answered `GET /api/editor/tags` with 500 "Tag database
not found" — the picker was empty for everyone who did not run from source. 2.9 MB of text.
"""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize("spec", ["pawpoller.spec", "pawpoller-server.spec"])
def test_the_build_ships_the_tag_database(spec):
    assert "('tag_database', 'tag_database')" in (ROOT / spec).read_text(encoding="utf-8")


def test_the_files_the_picker_reads_are_all_there():
    """The spec ships a folder; these are the names the API asks it for."""
    from routes import editor_api
    missing = [name for name, _label in editor_api._TAG_DB_FILES
               if name != "tag_database_user.txt"                     # written on first use
               and not (ROOT / "tag_database" / name).is_file()]
    assert not missing, missing
    assert (ROOT / "tag_database" / editor_api._TAG_ALIASES_FILE).is_file()
