"""MDEOL (4.88.9): saving a story from the editor keeps MASTER.md's line endings.

``write_text`` with the default ``newline`` writes CRLF on Windows, and ``read_text`` turns CRLF
into LF. So on a Windows desktop one save rewrote every line of an LF story as CRLF (and the
backup copy flipped too). The archive's stories are LF, so each save showed as a whole-file change.
"""
from __future__ import annotations

import asyncio

import pytest

from routes import editor_api


@pytest.fixture
def master(tmp_path, monkeypatch):
    md = tmp_path / "Sample_Story" / "Markdown"
    md.mkdir(parents=True)
    monkeypatch.setattr(editor_api, "get_archive_path", lambda: tmp_path)
    return md / "MASTER.md"


def _save(text):
    asyncio.run(editor_api.save_content("Sample_Story", editor_api.SaveRequest(content=text)))


@pytest.mark.parametrize("nl", ["\n", "\r\n"])
def test_save_keeps_the_files_own_line_endings(master, nl):
    master.write_bytes(f"# Old{nl}{nl}Line one.{nl}".encode())
    _save("# New\n\nLine one.\nLine two.\n")       # the browser always sends LF
    assert master.read_bytes() == f"# New{nl}{nl}Line one.{nl}Line two.{nl}".encode()


def test_crlf_from_the_page_does_not_double_up(master):
    master.write_bytes(b"old\r\n")
    _save("a\r\nb\r\n")
    assert master.read_bytes() == b"a\r\nb\r\n"


def test_a_new_file_is_lf(master):
    _save("a\nb\n")
    assert master.read_bytes() == b"a\nb\n"


def test_backup_is_an_exact_copy(master):
    original = b"# Old\n\nLine one.\n"
    master.write_bytes(original)
    backup = editor_api._backup_master(master)
    assert backup.read_bytes() == original
