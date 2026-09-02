"""Backup & restore (backlog Y, 2.171.0).

Restore is destructive, so these cover the round-trip (export → mutate →
restore-back), rejection of anything that isn't a PawPoller backup, and the
zip-slip guard. The backup module works off config.DATA_DIR, so the fixture
points that at a temp data dir with a fake DB + settings + one media file.
"""
import io
import json
import zipfile

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import config
from routes.backup_api import backup_router


@pytest.fixture
def client(tmp_path, monkeypatch):
    dd = tmp_path / "data"
    dd.mkdir()
    (dd / "pawpoller.db").write_bytes(b"SQLITE_FAKE_DB_v1")
    (dd / "settings.json").write_text('{"a": 1}', encoding="utf-8")
    (dd / "settings.vault.json").write_text('{"sekret": "x"}', encoding="utf-8")
    art = dd / "artwork" / "Piece"
    art.mkdir(parents=True)
    (art / "img.png").write_bytes(b"PNGDATA")
    monkeypatch.setattr(config, "DATA_DIR", dd)
    app = FastAPI()
    app.include_router(backup_router)
    return TestClient(app), dd


def test_export_contains_db_settings_media_and_manifest(client):
    c, _dd = client
    r = c.get("/api/backup/export")
    assert r.status_code == 200
    z = zipfile.ZipFile(io.BytesIO(r.content))
    names = set(z.namelist())
    assert {"manifest.json", "data/pawpoller.db", "data/settings.json",
            "data/settings.vault.json", "data/artwork/Piece/img.png"} <= names
    man = json.loads(z.read("manifest.json"))
    assert man["kind"] == "pawpoller-backup"
    assert "pawpoller.db" in man["files"] and "artwork" in man["dirs"]


def test_info_reports_items_and_size(client):
    c, _dd = client
    info = c.get("/api/backup/info").json()
    assert info["total_bytes"] > 0
    names = {i["name"] for i in info["items"]}
    assert "pawpoller.db" in names and "artwork/" in names


def test_import_rejects_non_backup_zip(client):
    c, _dd = client
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("random.txt", "nope")   # no manifest
    r = c.post("/api/backup/import",
               files={"file": ("x.zip", buf.getvalue(), "application/zip")})
    assert r.status_code == 400


def test_import_rejects_wrong_kind(client):
    c, _dd = client
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("manifest.json", json.dumps({"kind": "something-else"}))
        z.writestr("data/pawpoller.db", "x")
    r = c.post("/api/backup/import",
               files={"file": ("x.zip", buf.getvalue(), "application/zip")})
    assert r.status_code == 400


def test_round_trip_restore_and_safety_copy(client):
    c, dd = client
    exported = c.get("/api/backup/export").content
    # Mutate the live data so we can prove the restore reverts it.
    (dd / "pawpoller.db").write_bytes(b"MUTATED")
    (dd / "settings.json").write_text('{"a": 999}', encoding="utf-8")

    r = c.post("/api/backup/import",
               files={"file": ("b.zip", exported, "application/zip")})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] and "pawpoller.db" in body["restored"]

    # Data reverted to the exported state.
    assert (dd / "pawpoller.db").read_bytes() == b"SQLITE_FAKE_DB_v1"
    assert json.loads((dd / "settings.json").read_text())["a"] == 1
    # The pre-restore (mutated) state was preserved in the safety copy.
    safety = dd / body["safety_copy"]
    assert (safety / "pawpoller.db").read_bytes() == b"MUTATED"


def test_zip_slip_is_rejected(client):
    c, _dd = client
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("manifest.json", json.dumps({"kind": "pawpoller-backup"}))
        z.writestr("../evil.txt", "pwned")
    r = c.post("/api/backup/import",
               files={"file": ("e.zip", buf.getvalue(), "application/zip")})
    assert r.status_code == 400
