"""SEATFOLDERS: on a hosted seat the folder settings stay inside the seat's own storage.

Else a seat owner points the artwork folder at the container's root and reads it back through the mirror routes,
or at the turn folder every seat shares. Desktop and self-hosted servers are unchanged.
"""
import pytest
from fastapi import HTTPException

import config
import desktop_gate
from posting import artwork_reader, story_reader
from routes import backup_api


@pytest.fixture
def seat(tmp_path, monkeypatch):
    data, backups, shared = tmp_path / "data", tmp_path / "backups", tmp_path / "turns"
    for d in (data, backups, shared, data / "art"):
        d.mkdir()
    monkeypatch.setattr(config, "DATA_DIR", data)
    monkeypatch.setenv("PAWPOLLER_HOSTED", "1")
    monkeypatch.setenv("PAWPOLLER_AUTO_BACKUP_DIR", str(backups))
    return data, backups, shared


@pytest.mark.parametrize("key", ["artwork_archive_path", "posting_story_archive_path", "auto_backup_dir"])
def test_seat_refuses_outside_its_storage(seat, key):
    data, backups, shared = seat
    for bad in ("/", str(shared), str(data / ".." / "turns"), str(data.parent), 5):
        with pytest.raises(HTTPException) as e:
            desktop_gate.folder_or_400(key, bad)
        assert e.value.status_code == 400 and "hosted" in e.value.detail
    assert desktop_gate.folder_or_400(key, str(data / "art")) == str(data / "art")
    assert desktop_gate.folder_or_400(key, "") == ""                       # back to the usual folder


def test_box_backup_folder_only_for_backups(seat):
    _, backups, _ = seat
    assert desktop_gate.folder_or_400("auto_backup_dir", str(backups)) == str(backups)
    with pytest.raises(HTTPException):
        desktop_gate.folder_or_400("artwork_archive_path", str(backups))


def test_a_value_saved_before_is_ignored(seat):
    data, _, shared = seat
    config.save_settings({"artwork_archive_path": str(shared), "posting_story_archive_path": str(shared),
                          "auto_backup_dir": str(shared)})
    assert artwork_reader.get_artwork_archive_path() != shared
    assert story_reader.get_archive_path() != shared
    assert backup_api._auto_backup_dir() != shared


def test_not_a_seat_unchanged(tmp_path, monkeypatch):
    monkeypatch.delenv("PAWPOLLER_HOSTED", raising=False)
    assert desktop_gate.folder_or_400("artwork_archive_path", str(tmp_path)) == str(tmp_path)
    config.save_settings({"artwork_archive_path": str(tmp_path)})
    assert artwork_reader.get_artwork_archive_path() == tmp_path
