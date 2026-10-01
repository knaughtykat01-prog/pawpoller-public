"""4.45.5: /api/health/ready (CICDLEARN 5) and the backup folders (BACKUPGAPS)."""
from __future__ import annotations

import time
from collections import namedtuple

from fastapi.testclient import TestClient

import config


def _client():
    import dashboard
    return TestClient(dashboard.app, raise_server_exceptions=False)


def test_ready_answers_without_a_login(monkeypatch):
    monkeypatch.setattr(config, "is_dashboard_auth_required", lambda: True)
    r = _client().get("/api/health/ready")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok" and body["problems"] == [] and body["db_ms"] >= 0
    assert body["version"] == config.APP_VERSION and 0 <= body["disk_free_pct"] <= 100


def test_a_stalled_scheduler_or_full_disk_is_503(monkeypatch):
    from posting import scheduler
    monkeypatch.setattr(scheduler, "LAST_TICK", time.monotonic() - 3 * 3600)
    r = _client().get("/api/health/ready")
    assert r.status_code == 503 and r.json()["problems"] == ["posting scheduler stalled"]
    monkeypatch.setattr(scheduler, "LAST_TICK", time.monotonic())
    Usage = namedtuple("Usage", "total used free")
    monkeypatch.setattr("shutil.disk_usage", lambda p: Usage(100, 98, 2))
    r = _client().get("/api/health/ready")
    assert r.status_code == 503 and r.json()["problems"] == ["disk nearly full"]


def test_backup_zip_carries_the_new_folders(tmp_path, monkeypatch):
    import zipfile
    from routes import backup_api
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    for d in ("inbox", "promos", "commission_files", "podcasts", "stories"):
        (tmp_path / d).mkdir()
        (tmp_path / d / "one.bin").write_bytes(b"x")
    (tmp_path / "pawpoller.db").write_bytes(b"")
    manifest = backup_api.write_backup_zip(tmp_path / "out.zip")
    names = set(zipfile.ZipFile(tmp_path / "out.zip").namelist())
    for d in ("inbox", "promos", "commission_files", "podcasts", "stories"):
        assert f"data/{d}/one.bin" in names and d in manifest["dirs"]
