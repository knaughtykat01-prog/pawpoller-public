"""Scheduled automatic backups (gap G7).

Covers the due-check (enabled flag + interval) and that run_auto_backup writes a
zip into the configured folder and prunes to the retention count.
"""
from datetime import datetime, timezone
from pathlib import Path

import config
from routes import backup_api


def _seed_data_dir(dd: Path):
    dd.mkdir(parents=True, exist_ok=True)
    (dd / "pawpoller.db").write_bytes(b"DBDATA")
    (dd / "settings.json").write_text("{}", encoding="utf-8")


def test_auto_backup_due_gating():
    config.save_settings({"auto_backup_enabled": False})
    assert backup_api.auto_backup_due() is False

    # Enabled, never run → due.
    config.save_settings({"auto_backup_enabled": True, "auto_backup_interval_hours": 24})
    assert backup_api.auto_backup_due() is True

    # Just ran → not due.
    config.save_settings({"last_auto_backup_at": datetime.now(timezone.utc).isoformat()})
    assert backup_api.auto_backup_due() is False


def test_run_auto_backup_writes_zip_and_prunes(tmp_path, monkeypatch):
    dd = tmp_path / "data"
    _seed_data_dir(dd)
    out = tmp_path / "backups"
    out.mkdir()
    monkeypatch.setattr(config, "DATA_DIR", dd)
    config.save_settings({"auto_backup_dir": str(out), "auto_backup_keep": 3})

    # Pre-create four older backups (distinct, sortable stamps).
    for stamp in ("20200101-000001", "20200101-000002", "20200101-000003", "20200101-000004"):
        (out / f"pawpoller-backup-{stamp}.zip").write_bytes(b"OLD")

    result = backup_api.run_auto_backup()

    # A new, real zip was written.
    assert Path(result["path"]).is_file()
    assert result["bytes"] > 0

    # keep=3 → only the newest three remain (the new one + the two newest olds).
    remaining = sorted(p.name for p in out.glob("pawpoller-backup-*.zip"))
    assert len(remaining) == 3
    # The two oldest were pruned.
    assert "pawpoller-backup-20200101-000001.zip" not in remaining
    assert "pawpoller-backup-20200101-000002.zip" not in remaining
    # The newest olds survived.
    assert "pawpoller-backup-20200101-000004.zip" in remaining
