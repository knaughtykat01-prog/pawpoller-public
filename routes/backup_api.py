"""Backup & restore (backlog Y) — "download my everything" / "restore from file".

A backup is a .zip of the user's own data under DATA_DIR: the SQLite database
(all analytics, publications, masterpieces, links…), settings.json + the
encrypted credential vault, and the app-managed media folders (artwork,
posts_media, stories, inbox, promos, commission_files, podcasts, and the
story-archive when it lives under DATA_DIR). Logs and
transient caches (ig_media) are excluded.

Restore is DESTRUCTIVE — it replaces the DB + settings + vault and merges media
over the current data — so it writes a timestamped safety copy of the current
critical files first, guards against zip-slip, and tells the user to restart
(get_settings() re-reads from disk, and get_connection() opens the DB fresh, but
module-level constants + long-lived singletons only re-read on restart).
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

import config

logger = logging.getLogger(__name__)
backup_router = APIRouter(prefix="/api/backup", tags=["backup"])

BACKUP_KIND = "pawpoller-backup"
_MAX_BACKUP_BYTES = 2 * 1024 * 1024 * 1024      # 2 GB restore-upload cap

# Relative to DATA_DIR. Files are replaced on restore; dirs are merged (restored
# files overwrite same-named, existing extras are left alone — never a blind
# media wipe). ig_media (transient IG image stash) is deliberately not included.
_BACKUP_FILES = ["pawpoller.db", "settings.json", "settings.vault.json"]
# BACKUPGAPS (4.45.5): the other folders of the operator's own work — unused uploads, promos,
# commission files, podcasts, and stories kept under data/. `datamap` routes each path here as
# "files"; `tests/test_data_classification.py::test_backup_dirs_match_the_registry` holds the two together.
_BACKUP_DIRS = ["artwork", "posts_media", "story-archive", "stories", "inbox", "promos",
                "commission_files", "podcasts"]


def _data_dir() -> Path:
    return Path(config.DATA_DIR)


def _dir_size(p: Path) -> int:
    return sum(x.stat().st_size for x in p.rglob("*") if x.is_file())


@backup_router.get("/info")
def backup_info():
    """What a backup would contain + its rough size, for the Settings UI."""
    dd = _data_dir()
    items, total = [], 0
    for f in _BACKUP_FILES:
        p = dd / f
        if p.is_file():
            sz = p.stat().st_size
            total += sz
            items.append({"name": f, "bytes": sz})
    for d in _BACKUP_DIRS:
        p = dd / d
        if p.is_dir():
            sz = _dir_size(p)
            total += sz
            items.append({"name": d + "/", "bytes": sz})
    return {"items": items, "total_bytes": total, "app_version": config.APP_VERSION}


def _db_snapshot(into: Path) -> Path:
    """A consistent copy of the live database via SQLite's backup API.

    ⚠ Zipping ``pawpoller.db`` directly (as backups did until 4.38.0) copies a file
    another thread may be writing, and the WAL holds commits the main file does not
    have yet — a backup that restores to an older or torn database.
    """
    import sqlite3
    live = _data_dir() / "pawpoller.db"
    out = into / "pawpoller.db"
    try:
        src = sqlite3.connect(str(live))
        try:
            dst = sqlite3.connect(str(out))
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
    except sqlite3.DatabaseError:
        # Not something SQLite can open: copy the bytes as backups always did,
        # rather than fail the backup outright.
        logger.warning("Backup: database copied as a plain file (SQLite could not open it).")
        out.unlink(missing_ok=True)
        shutil.copy2(live, out)
    return out


def _write_core(z: zipfile.ZipFile, manifest: dict, tmp: Path) -> None:
    """The small, critical part of every backup: database, settings, vault."""
    dd = _data_dir()
    for f in _BACKUP_FILES:
        p = dd / f
        if not p.is_file():
            continue
        if f == "pawpoller.db":
            p = _db_snapshot(tmp)
        z.write(p, f"data/{f}")
        manifest["files"].append(f)


def write_backup_zip(dest: Path, include_media: bool = True) -> dict:
    """Build a backup .zip at `dest` and return its manifest. The HTTP export is a
    full backup ("download my everything"); the scheduled one leaves media out and
    mirrors it incrementally instead (see ``sync_media_mirror``)."""
    dd = _data_dir()
    manifest = {
        "kind": BACKUP_KIND,
        "app_version": config.APP_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "files": [], "dirs": [],
    }
    with tempfile.TemporaryDirectory(prefix="pawpoller-db-") as tmp, \
            zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        _write_core(z, manifest, Path(tmp))
        if include_media:
            for d in _BACKUP_DIRS:
                p = dd / d
                if not p.is_dir():
                    continue
                manifest["dirs"].append(d)
                for x in p.rglob("*"):
                    if x.is_file():
                        # Images are already compressed; deflating them again
                        # costs CPU for ~0% saving.
                        z.write(x, f"data/{x.relative_to(dd).as_posix()}",
                                compress_type=zipfile.ZIP_STORED)
        else:
            manifest["media_mirror"] = MEDIA_MIRROR
        z.writestr("manifest.json", json.dumps(manifest, indent=2))
    return manifest


MEDIA_MIRROR = "media"


def sync_media_mirror(out_dir: Path) -> dict:
    """Copy media into ``out_dir/media`` — only files that are new or changed.

    This is what makes nightly backups cheap: until 4.38.0 every night re-zipped
    every image (~200 MB a night, ~3.4x the artwork itself across the kept zips).
    Images barely compress, so the zips were near-identical copies.

    ⚠ A file deleted from the library is NOT deleted from the mirror. A backup
    that follows deletions stops protecting against exactly the mistake it is for.
    """
    dd = _data_dir()
    root = out_dir / MEDIA_MIRROR
    copied = total = 0
    for d in _BACKUP_DIRS:
        src_root = dd / d
        if not src_root.is_dir():
            continue
        for src in src_root.rglob("*"):
            if not src.is_file():
                continue
            total += 1
            dst = root / src.relative_to(dd)
            st = src.stat()
            try:
                dt = dst.stat()
                if dt.st_size == st.st_size and int(dt.st_mtime) == int(st.st_mtime):
                    continue
            except FileNotFoundError:
                pass
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            copied += 1
    return {"files": total, "copied": copied}

# ── Scheduled automatic backups (gap G7) ─────────────────────────────────────
# A daemon thread (started by main.py + server.py) periodically writes a
# timestamped backup into a configured folder and prunes to a retention count.
# The check self-throttles on `last_auto_backup_at`, so the thread can tick
# often and cheaply. Off by default — opt in from Settings → Data.
_AUTO_DEFAULTS = {"interval_hours": 24, "keep": 7}


def _auto_backup_dir() -> Path:
    s = config.get_settings()
    return Path(s.get("auto_backup_dir") or str(_data_dir() / "auto-backups"))


def run_auto_backup() -> dict:
    """Write a timestamped backup into the auto-backup folder and prune to the
    retention count. Returns {path, bytes, pruned:[names]}."""
    out_dir = _auto_backup_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    dest = out_dir / f"pawpoller-backup-{stamp}.zip"
    # Media first: a zip that exists means its mirror was complete when written.
    media = sync_media_mirror(out_dir)
    write_backup_zip(dest, include_media=False)
    # Timestamped names sort chronologically, so keep the newest N by sort order.
    keep = max(1, int(config.get_settings().get("auto_backup_keep", _AUTO_DEFAULTS["keep"]) or 7))
    existing = sorted(out_dir.glob("pawpoller-backup-*.zip"))
    pruned = []
    for old in existing[:-keep]:
        try:
            old.unlink()
            pruned.append(old.name)
        except OSError:
            pass
    return {"path": str(dest), "bytes": dest.stat().st_size, "pruned": pruned, "media": media}


def auto_backup_due() -> bool:
    s = config.get_settings()
    if not s.get("auto_backup_enabled", False):
        return False
    interval_h = max(1, int(s.get("auto_backup_interval_hours", _AUTO_DEFAULTS["interval_hours"]) or 24))
    last = s.get("last_auto_backup_at")
    if not last:
        return True
    try:
        last_dt = datetime.fromisoformat(last)
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - last_dt).total_seconds() >= interval_h * 3600
    except (ValueError, TypeError):
        return True


def maybe_run_auto_backup() -> dict | None:
    """Run a backup iff enabled + due, then stamp the time. Safe to call often —
    self-throttles. Returns the result, or None when not due / disabled."""
    if not auto_backup_due():
        return None
    try:
        result = run_auto_backup()
        config.save_settings({"last_auto_backup_at": datetime.now(timezone.utc).isoformat()})
        logger.info("Auto-backup written: %s (%d bytes, pruned %d old)",
                    result["path"], result["bytes"], len(result["pruned"]))
        return result
    except Exception as e:
        logger.error("Auto-backup failed: %s", e, exc_info=True)
        return None


def run_auto_backup_scheduler():
    """Blocking daemon entry point (started as a thread by main.py + server.py).
    Ticks every 30 min; each tick self-throttles on the enabled flag +
    last_auto_backup_at, so it's near-free when off or not yet due."""
    import time as _time
    _time.sleep(120)  # let settings seed on startup
    while True:
        try:
            maybe_run_auto_backup()
        except Exception as e:
            logger.warning("Auto-backup scheduler tick failed: %s", e)
        try:
            maybe_prune_snapshots()
        except Exception as e:
            logger.warning("Snapshot thinning failed: %s", type(e).__name__)
        _time.sleep(30 * 60)


def maybe_prune_snapshots() -> dict | None:
    """Thin old stats snapshots once a day (``database/snapshot_prune.py``).

    Rides the backup thread because it is daily housekeeping that must run in both
    entry points, and this thread already does (main.py + server.py). Runs whether
    or not auto-backup is enabled."""
    s = config.get_settings()
    last = s.get("last_snapshot_prune_at")
    if last:
        try:
            last_dt = datetime.fromisoformat(last)
            if (datetime.now(timezone.utc) - last_dt).total_seconds() < 24 * 3600:
                return None
        except (ValueError, TypeError):
            pass
    from database.db import get_connection
    from database import snapshot_prune
    conn = get_connection()
    try:
        removed = snapshot_prune.prune(conn)
    finally:
        conn.close()
    config.save_settings({"last_snapshot_prune_at": datetime.now(timezone.utc).isoformat()})
    return removed


@backup_router.get("/auto")
def auto_backup_status():
    """Current auto-backup config + last-run time, for Settings → Data."""
    s = config.get_settings()
    return {
        "enabled": bool(s.get("auto_backup_enabled", False)),
        "interval_hours": int(s.get("auto_backup_interval_hours", _AUTO_DEFAULTS["interval_hours"]) or 24),
        "keep": int(s.get("auto_backup_keep", _AUTO_DEFAULTS["keep"]) or 7),
        "dir": str(_auto_backup_dir()),
        "last_at": s.get("last_auto_backup_at"),
    }


@backup_router.post("/auto")
def auto_backup_config(body: dict):
    """Persist auto-backup settings; optionally run one immediately (run_now)."""
    updates: dict = {}
    if "enabled" in body:
        updates["auto_backup_enabled"] = bool(body["enabled"])
    if body.get("interval_hours") is not None:
        updates["auto_backup_interval_hours"] = max(1, int(body["interval_hours"]))
    if body.get("keep") is not None:
        updates["auto_backup_keep"] = max(1, int(body["keep"]))
    if body.get("dir"):
        updates["auto_backup_dir"] = str(body["dir"]).strip()
    if updates:
        config.save_settings(updates)
    ran = None
    if body.get("run_now"):
        ran = run_auto_backup()
        config.save_settings({"last_auto_backup_at": datetime.now(timezone.utc).isoformat()})
    return {"ok": True, "ran": ran, **auto_backup_status()}


@backup_router.get("/export")
def export_backup():
    """Stream a .zip of the user's data. Includes the credential vault — it's a
    full backup of the user's own instance; the UI warns that it holds secrets."""
    fd, tmp = tempfile.mkstemp(suffix=".zip", prefix="pawpoller-backup-")
    os.close(fd)
    try:
        write_backup_zip(Path(tmp))
    except Exception as e:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        logger.error("Backup export failed: %s", e, exc_info=True)
        raise HTTPException(500, detail=f"Backup failed: {e}")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return FileResponse(
        tmp, media_type="application/zip",
        filename=f"pawpoller-backup-{stamp}.zip",
        background=BackgroundTask(lambda: os.path.exists(tmp) and os.unlink(tmp)),
    )


def _safe_extract(z: zipfile.ZipFile, dest: Path) -> None:
    """Extract with a zip-slip guard — refuse any member that would escape dest."""
    dest = dest.resolve()
    for member in z.namelist():
        target = (dest / member).resolve()
        if dest != target and dest not in target.parents:
            raise HTTPException(400, detail="Unsafe path in backup archive.")
    z.extractall(dest)


def _merge_tree(src: Path, dst: Path) -> None:
    dst.mkdir(parents=True, exist_ok=True)
    for item in src.rglob("*"):
        target = dst / item.relative_to(src)
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)


@backup_router.post("/import")
async def import_backup(file: UploadFile = File(...)):
    """Restore from a backup .zip. DESTRUCTIVE — replaces DB + settings + vault
    and merges media over the current data. A timestamped safety copy of the
    current critical files is written first; a restart is required to finish."""
    data = await file.read()
    if not data:
        raise HTTPException(400, detail="Empty upload.")
    if len(data) > _MAX_BACKUP_BYTES:
        raise HTTPException(413, detail="Backup file exceeds the 2 GB limit.")

    dd = _data_dir()
    tmpdir = Path(tempfile.mkdtemp(prefix="pawpoller-restore-"))
    try:
        zpath = tmpdir / "upload.zip"
        zpath.write_bytes(data)
        try:
            with zipfile.ZipFile(zpath) as z:
                _safe_extract(z, tmpdir)
        except zipfile.BadZipFile:
            raise HTTPException(400, detail="Not a valid .zip file.")

        manifest_p = tmpdir / "manifest.json"
        if not manifest_p.is_file():
            raise HTTPException(400, detail="Not a PawPoller backup (no manifest).")
        try:
            manifest = json.loads(manifest_p.read_text("utf-8"))
        except ValueError:
            raise HTTPException(400, detail="Backup manifest is unreadable.")
        if manifest.get("kind") != BACKUP_KIND:
            raise HTTPException(400, detail="This isn't a PawPoller backup.")

        src_data = tmpdir / "data"
        if not (src_data / "pawpoller.db").is_file():
            raise HTTPException(400, detail="Backup is missing the database.")

        # Safety copy of the current critical state BEFORE overwriting anything.
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        safety = dd / f"restore-safety-{stamp}"
        safety.mkdir(parents=True, exist_ok=True)
        for f in _BACKUP_FILES:
            cur = dd / f
            if cur.is_file():
                shutil.copy2(cur, safety / f)

        restored = []
        for f in _BACKUP_FILES:
            sp = src_data / f
            if sp.is_file():
                shutil.copy2(sp, dd / f)
                restored.append(f)
        # A scheduled backup carries no media; it lives in the mirror beside the
        # zips on this machine. Restore from it when the zip names one.
        # RESTOREMIRROR (4.46.2): the name comes from the uploaded file, so only OUR mirror name is
        # honoured — anything else could point the restore at same-named folders elsewhere on disk.
        mirror = _auto_backup_dir() / MEDIA_MIRROR if manifest.get("media_mirror") == MEDIA_MIRROR else None
        for d in _BACKUP_DIRS:
            sd = src_data / d
            if not sd.is_dir() and mirror is not None and (mirror / d).is_dir():
                sd = mirror / d
            if sd.is_dir():
                _merge_tree(sd, dd / d)
                restored.append(d + "/")

        logger.info("Backup restored (%s); safety copy at %s", ", ".join(restored), safety.name)
        return {
            "ok": True,
            "restored": restored,
            "safety_copy": safety.name,
            "app_version": manifest.get("app_version", ""),
            "message": "Restored. Restart PawPoller to finish loading the restored data.",
        }
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
