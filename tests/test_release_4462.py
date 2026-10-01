"""4.46.2: the three Lows from the 4.46.0/4.46.1 release reviews."""
from __future__ import annotations

import asyncio
import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient

import config


@pytest.fixture
def local(monkeypatch):
    import dashboard
    monkeypatch.setattr(config, "is_dashboard_auth_required", lambda: False)
    return TestClient(dashboard.app, raise_server_exceptions=False, client=("127.0.0.1", 50000))


def test_restore_ignores_a_foreign_media_mirror_name(local, tmp_path, monkeypatch):
    """RESTOREMIRROR: a backup naming some other folder as its media mirror restores no media from it."""
    from routes import backup_api
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(config, "DATA_DIR", data)
    monkeypatch.setattr(backup_api, "_auto_backup_dir", lambda: tmp_path / "auto")
    elsewhere = tmp_path / "auto" / ".." / "elsewhere"
    (tmp_path / "elsewhere" / "artwork").mkdir(parents=True)
    (tmp_path / "elsewhere" / "artwork" / "secret.txt").write_text("x")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("manifest.json", json.dumps({"kind": backup_api.BACKUP_KIND, "media_mirror": "../elsewhere"}))
        z.writestr("data/pawpoller.db", b"")
    r = local.post("/api/backup/import", files={"file": ("b.zip", buf.getvalue(), "application/zip")})
    assert r.status_code == 200, r.text
    assert not (data / "artwork" / "secret.txt").exists()
    assert elsewhere.resolve().exists()


def test_the_linked_telegram_post_respects_never_post(monkeypatch):
    """TGNEVERPOST: the linked announcement builds its poster through the manager's guard."""
    from database import accounts as adb
    from database.db import get_connection
    from posting import tg_linked
    conn = get_connection()
    try:
        aid = adb.create_account(conn, "tg", "Someone else's channel")
    finally:
        conn.close()
    config.save_settings({"never_post_account_ids": [aid]})
    monkeypatch.setattr(tg_linked, "find_live_links", lambda *a, **k: [("fa", "https://example.org/view/1")])
    monkeypatch.setattr(tg_linked, "build_announcement", lambda **k: "text")
    r = asyncio.run(tg_linked.announce_existing(None, story_name="Sample Story", account_id=aid))
    assert r["status"] == "error" and "Never post" in r["error"]


def test_a_saved_picarto_name_is_always_clean(local, monkeypatch):
    import clients.pic.client as mod

    class Fake:
        channel = "Picarto"

        def __init__(self, name="", base_url=""):
            pass

        async def get_channel(self, refresh=False):
            return {"name": "bad name/../x", "adult": False}

        async def close(self):
            pass
    monkeypatch.setattr(mod, "PicClient", Fake)
    r = local.post("/api/pic/channel", json={"channel": "Picarto"})
    assert r.status_code == 400
    assert not config.get_settings().get("pic_channel")
