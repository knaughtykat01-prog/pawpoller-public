"""4.38.0 storage + bandwidth: thumbnails, incremental backups, snapshot thinning.

Pinned: a thumbnail never touches the original and falls back to it; a scheduled
backup carries no media, re-copies nothing unchanged and still restores; thinning
keeps every recent row and one row per old day, and deletes nothing else.
"""
from __future__ import annotations

import json
import sqlite3
import zipfile
from pathlib import Path

import pytest
from PIL import Image

import config


# ── thumbnails ───────────────────────────────────────────────────────────────

def _png(path: Path, w=1600, h=1200) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (w, h), (120, 60, 30)).save(path)
    return path


def test_a_thumbnail_is_small_webp_and_leaves_the_original_alone(tmp_path, monkeypatch):
    import thumbs
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    src = _png(tmp_path / "art" / "big.png")
    before = src.read_bytes()
    t = thumbs.thumbnail(src, 400)
    assert t is not None and t.suffix == ".webp"
    with Image.open(t) as im:
        assert im.width == 400
    assert src.read_bytes() == before, "the original must never change"
    assert thumbs.thumbnail(src, 400) == t, "made once, then reused"


def test_a_changed_original_gets_a_new_thumbnail(tmp_path, monkeypatch):
    import os
    import thumbs
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    src = _png(tmp_path / "art" / "big.png")
    first = thumbs.thumbnail(src, 400)
    _png(src, 1800, 900)
    os.utime(src, (1, 1))
    assert thumbs.thumbnail(src, 400) != first


def test_small_or_unreadable_images_fall_back_to_the_original(tmp_path, monkeypatch):
    import thumbs
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    assert thumbs.thumbnail(_png(tmp_path / "s.png", 100, 100), 400) is None
    bad = tmp_path / "bad.png"
    bad.write_bytes(b"not an image")
    assert thumbs.thumbnail(bad, 400) is None


def test_widths_snap_to_a_few_sizes():
    import thumbs
    assert [thumbs.snap(w) for w in (1, 250, 400, 9999, "x")] == [200, 400, 400, 800, 400]


def test_grids_ask_for_thumbnails_and_viewers_do_not():
    """The Library's links come from the server; the viewer and posting must keep
    asking for the original. (artwork.js's hub grid, the third caller, was removed
    in 4.45.5.)"""
    for path, needle in (("routes/submissions_api.py", "&w=400"), ("routes/api.py", "&w=400"),
                         ("frontend/js/work_picker.js", "&w=400")):
        assert needle in open(path, encoding="utf-8").read(), path
    reader = open("posting/artwork_reader.py", encoding="utf-8").read()
    assert "thumbs" not in reader, "posting must read originals"


# ── incremental backups ──────────────────────────────────────────────────────

@pytest.fixture
def data(tmp_path, monkeypatch):
    from routes import backup_api
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(backup_api, "_auto_backup_dir", lambda: tmp_path / "auto-backups")
    db = sqlite3.connect(str(tmp_path / "pawpoller.db"))
    db.execute("CREATE TABLE t (x)")
    db.execute("INSERT INTO t VALUES (42)")
    db.commit()
    db.close()
    (tmp_path / "settings.json").write_text("{}", encoding="utf-8")
    _png(tmp_path / "artwork" / "Sample_Piece" / "image.png", 300, 300)
    return tmp_path


def test_a_scheduled_backup_zip_carries_no_media(data):
    from routes import backup_api
    r = backup_api.run_auto_backup()
    with zipfile.ZipFile(r["path"]) as z:
        names = z.namelist()
        manifest = json.loads(z.read("manifest.json"))
    assert "data/pawpoller.db" in names
    assert not any(n.startswith("data/artwork/") for n in names)
    assert manifest["media_mirror"] == "media"
    assert (data / "auto-backups" / "media" / "artwork" / "Sample_Piece" / "image.png").is_file()


def test_the_second_backup_copies_nothing_unchanged(data):
    from routes import backup_api
    backup_api.run_auto_backup()
    assert backup_api.run_auto_backup()["media"]["copied"] == 0


def test_a_file_deleted_from_the_library_stays_in_the_mirror(data):
    from routes import backup_api
    backup_api.run_auto_backup()
    (data / "artwork" / "Sample_Piece" / "image.png").unlink()
    backup_api.run_auto_backup()
    assert (data / "auto-backups" / "media" / "artwork" / "Sample_Piece" / "image.png").is_file()


def test_the_database_in_a_backup_is_a_real_snapshot(data, tmp_path):
    from routes import backup_api
    r = backup_api.run_auto_backup()
    with zipfile.ZipFile(r["path"]) as z:
        z.extract("data/pawpoller.db", tmp_path / "x")
    c = sqlite3.connect(str(tmp_path / "x" / "data" / "pawpoller.db"))
    assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert c.execute("SELECT x FROM t").fetchone()[0] == 42


def test_restoring_a_scheduled_backup_brings_media_back_from_the_mirror(data):
    import asyncio
    import io

    from fastapi import UploadFile
    from routes import backup_api
    r = backup_api.run_auto_backup()
    (data / "artwork" / "Sample_Piece" / "image.png").unlink()
    up = UploadFile(file=io.BytesIO(Path(r["path"]).read_bytes()), filename="b.zip")
    out = asyncio.run(backup_api.import_backup(up))
    assert "artwork/" in out["restored"]
    assert (data / "artwork" / "Sample_Piece" / "image.png").is_file()


def test_the_manual_export_is_still_everything(data, tmp_path):
    from routes import backup_api
    dest = tmp_path / "full.zip"
    backup_api.write_backup_zip(dest)
    with zipfile.ZipFile(dest) as z:
        assert any(n.startswith("data/artwork/") for n in z.namelist())


# ── snapshot thinning ────────────────────────────────────────────────────────

def test_thinning_keeps_recent_rows_and_one_per_old_day():
    from database import snapshot_prune
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE x_snapshots (id INTEGER PRIMARY KEY, submission_id, polled_at, views)")
    c.execute("CREATE TABLE y_snapshots (id INTEGER PRIMARY KEY, account_id, submission_id, polled_at)")
    c.execute("CREATE TABLE not_stats (id INTEGER PRIMARY KEY, polled_at)")
    rows = [("A", "2020-01-01 01:00:00", 1), ("A", "2020-01-01 09:00:00", 2),
            ("A", "2020-01-02 01:00:00", 3), ("B", "2020-01-01 05:00:00", 4)]
    c.executemany("INSERT INTO x_snapshots (submission_id, polled_at, views) VALUES (?, ?, ?)", rows)
    c.execute("INSERT INTO x_snapshots (submission_id, polled_at, views) "
              "VALUES ('A', datetime('now'), 5), ('A', datetime('now'), 6)")
    c.executemany("INSERT INTO y_snapshots (account_id, submission_id, polled_at) VALUES (?, ?, ?)",
                  [(1, "A", "2020-01-01 01:00:00"), (2, "A", "2020-01-01 02:00:00")])
    c.execute("INSERT INTO not_stats (polled_at) VALUES ('2000-01-01')")
    removed = snapshot_prune.prune(c)
    views = sorted(r[0] for r in c.execute("SELECT views FROM x_snapshots"))
    assert views == [2, 3, 4, 5, 6], "last of each old day, and every recent row"
    assert c.execute("SELECT COUNT(*) FROM y_snapshots").fetchone()[0] == 2, \
        "different accounts are different series"
    assert c.execute("SELECT COUNT(*) FROM not_stats").fetchone()[0] == 1
    assert removed == {"x_snapshots": 1}


def test_every_real_snapshot_table_is_found(db_conn):
    from database import snapshot_prune
    found = {t for t, _ in snapshot_prune.snapshot_tables(db_conn)}
    assert {"snapshots", "fa_snapshots", "tw_snapshots", "e621_snapshots"} <= found
