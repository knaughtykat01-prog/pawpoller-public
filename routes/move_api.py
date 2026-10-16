"""Move a whole PawPoller into another install, e.g. a hosted seat (4.71.0, spec 035).

Why a backup + restore can't do this (checked against a real library, 2026-10-13):
  * a seat sits behind Cloudflare, which refuses any upload over 100 MB; a real backup is ~320 MB;
  * a backup carries the vault but not its key, and every install has its own key. The sign-in
    lives in that vault, so a seat restored from someone else's backup can't read its own account
    (locked out) and every site login is lost;
  * a restore asks for a restart, and nobody in a seat can restart it.

So the old install makes a *moving file*: the library as a backup would hold it, the site logins
sealed with a phrase the person chooses (scrypt → Fernet), and nothing that belongs to the old
machine (its sign-in, pairing, folders, device role — KEEP_HERE). The new install takes it in
pieces under 100 MB, opens the logins with the phrase, keeps its OWN KEEP_HERE values, re-locks the
logins under its own vault key, copies the database in with SQLite's backup API, merges media
(never deletes) and, in a container, exits so Docker brings it back.

Logs carry sizes and outcomes only — never the phrase, a login or a file name from the library.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import secrets
import shutil
import sqlite3
import threading
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse

import config

logger = logging.getLogger(__name__)


def _person_only(request: Request) -> None:
    """A move is the account owner's act in a browser, never a paired install's API key."""
    if request.headers.get("Authorization", "").startswith("Bearer "):
        raise HTTPException(403, detail="Moving is done signed in, in the browser.")


move_router = APIRouter(prefix="/api/move", tags=["move"], dependencies=[Depends(_person_only)])

MOVE_KIND = "pawpoller-move"
PIECE_BYTES = 32 * 1024 * 1024          # each upload request stays far below Cloudflare's 100 MB
MIN_PHRASE = 12
STALE_SECONDS = 24 * 3600
ARRIVED_KEEP = 7 * 24 * 3600            # 4.75.0 (spec 036): a move that was SENT here waits a week for its owner
_SCRYPT = {"n": 2 ** 15, "r": 8, "p": 1}   # ~32 MB and ~0.1 s per guess
_ID_RE = re.compile(r"[0-9a-f]{32}")

# Media folders under DATA_DIR that travel as they are. Artwork and stories are read from wherever
# THIS install keeps them (a desktop may point elsewhere; the VM's stories sit outside DATA_DIR).
_PLAIN_DIRS = ("posts_media", "stories", "inbox", "promos", "commission_files", "podcasts")
_DIRS = ("artwork", "story-archive") + _PLAIN_DIRS

# What belongs to the machine, not the library: never leaves in a moving file, and the receiving
# install keeps its own values (present or absent). Pairing already keeps SYNC_EXCLUDE per-install.
# The backup keys matter most: a seat's env only SEEDS missing settings, so an imported
# auto_backup_enabled=false would silently stop the seat's nightly backup.
KEEP_HERE = frozenset(config.SYNC_EXCLUDE) | frozenset({
    "dashboard_user", "dashboard_password",              # the old login scheme
    "posting_server_url", "posting_server_api_key",      # pairing with another install
    "auto_sync_enabled",
    "auto_backup_enabled", "auto_backup_keep", "auto_backup_interval_hours",
    "last_auto_backup_at", "last_snapshot_prune_at",
    "turnstile_site_key", "turnstile_secret_key",        # this install's sign-in guard
    "consent_records", "tech_reports", "tech_usage",     # consent is given per install
    "moved_away_at", "moved_away_restore",
    "cf_worker_url", "cf_worker_key",                    # 4.72.0 (spec 037): the old install's relay isn't the new one's
})


def keeps_here(key: str) -> bool:
    return key in KEEP_HERE or key.startswith("auth_")


# ── Sealing the logins ───────────────────────────────────────────────────────

def _kdf(phrase: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    raw = Scrypt(salt=salt, length=32, n=n, r=r, p=p).derive(phrase.encode("utf-8"))
    return base64.urlsafe_b64encode(raw)


def seal(creds: dict, phrase: str) -> dict:
    from cryptography.fernet import Fernet
    salt = secrets.token_bytes(16)
    token = Fernet(_kdf(phrase, salt, **_SCRYPT)).encrypt(json.dumps(creds).encode("utf-8"))
    return {"v": 1, "kdf": "scrypt", **_SCRYPT, "salt": base64.b64encode(salt).decode(), "token": token.decode()}


class WrongPhrase(Exception):
    pass


def open_sealed(blob: dict, phrase: str) -> dict:
    from cryptography.fernet import Fernet, InvalidToken
    try:
        if blob.get("v") != 1 or blob.get("kdf") != "scrypt":
            raise WrongPhrase()
        n, r, p = int(blob["n"]), int(blob["r"]), int(blob["p"])
        if (n, r, p) != (_SCRYPT["n"], _SCRYPT["r"], _SCRYPT["p"]):   # a file can't make us burn memory
            raise WrongPhrase()
        key = _kdf(phrase, base64.b64decode(blob["salt"]), n, r, p)
        return json.loads(Fernet(key).decrypt(blob["token"].encode()))
    except (InvalidToken, KeyError, ValueError, TypeError):
        raise WrongPhrase() from None


def _check_phrase(phrase) -> str:
    if not isinstance(phrase, str) or len(phrase) < MIN_PHRASE:
        raise HTTPException(400, detail=f"The moving phrase needs at least {MIN_PHRASE} characters.")
    return phrase


# ── Where things live ────────────────────────────────────────────────────────

def _data_dir() -> Path:
    return Path(config.DATA_DIR)


def _dir_for(name: str) -> Path:
    """This install's folder for one of _DIRS."""
    if name == "artwork":
        from posting import artwork_reader
        return Path(artwork_reader.get_artwork_archive_path())
    if name == "story-archive":
        from posting import story_reader
        return Path(story_reader.get_archive_path())
    return _data_dir() / name


def _db_copy(dest: Path) -> None:
    """A consistent copy of the live database (SQLite backup API, safe under WAL)."""
    src = sqlite3.connect(str(config.DB_PATH))
    try:
        dst = sqlite3.connect(str(dest))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def _version_tuple(v: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", str(v))[:3])


# ── Making the moving file (the old install) ────────────────────────────────

def _moving_settings() -> tuple[dict, dict, str]:
    """(plain settings, logins, logins state) as they should travel."""
    s = config.get_settings()
    state = "unreadable" if getattr(config, "_vault_unreadable", False) else "sealed"
    plain = {k: v for k, v in s.items() if not config.is_credential_key(k) and not keeps_here(k)}
    creds = {k: v for k, v in s.items() if config.is_credential_key(k) and not keeps_here(k) and v}
    # A copy that is already paused from an earlier move travels as it was BEFORE the pause,
    # or the new install would arrive paused too.
    plain.update(s.get("moved_away_restore") or {})
    if state == "sealed" and not creds:
        state = "none"
    return plain, creds, state


def _media_files():
    """(arcname, path) for every media file that travels. A folder that is also another one (stories
    kept in data/stories, say) travels once, under the first name."""
    seen = set()
    for name in _DIRS:
        root = _dir_for(name)
        if not root.is_dir() or root.resolve() in seen:
            continue
        seen.add(root.resolve())
        for p in root.rglob("*"):
            if p.is_file():
                yield f"data/{name}/{p.relative_to(root).as_posix()}", p


def write_move_zip(dest: Path, phrase: str, progress=None) -> dict:
    plain, creds, state = _moving_settings()
    media = list(_media_files())
    total = sum(p.stat().st_size for _, p in media)
    manifest = {"kind": MOVE_KIND, "v": 1, "app_version": config.APP_VERSION,
                "created_at": datetime.now(timezone.utc).isoformat(), "logins": state,
                "from_tag": str(config.get_settings().get("trello_instance_tag") or ""),
                "dirs": sorted({a.split("/")[1] for a, _ in media}), "files": len(media), "bytes": total}
    done = 0
    tmp_db = dest.with_name(dest.name + ".db")
    try:
        _db_copy(tmp_db)
        with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(tmp_db, "data/pawpoller.db")
            z.writestr("data/settings.json", json.dumps(plain, indent=2))
            if state == "sealed":
                z.writestr("logins.sealed", json.dumps(seal(creds, phrase)))
            for arc, p in media:
                z.write(p, arc, compress_type=zipfile.ZIP_STORED)   # pictures don't compress
                done += p.stat().st_size
                if progress:
                    progress(done, total)
            z.writestr("manifest.json", json.dumps(manifest, indent=2))
    finally:
        tmp_db.unlink(missing_ok=True)
    return manifest


_EXPORT: dict = {"state": "idle"}
_EXPORT_LOCK = threading.Lock()


def _out_dir() -> Path:
    return _data_dir() / "move-out"


def _pause_this_copy() -> None:
    s = config.get_settings()
    if s.get("moved_away_at"):
        return
    config.save_settings({
        "moved_away_at": datetime.now(timezone.utc).isoformat(),
        "moved_away_restore": {"polling_paused": bool(s.get("polling_paused", False)),
                               "posting_enabled": bool(s.get("posting_enabled", False)),
                               "auto_sync_enabled": bool(s.get("auto_sync_enabled", False))},
        "polling_paused": True, "posting_enabled": False, "auto_sync_enabled": False,
    })
    logger.info("Move: this copy is paused (polling, posting and sync off) after making a moving file.")


def _run_export(phrase: str, pause: bool) -> None:
    out = _out_dir()
    paused_now = False
    try:
        shutil.rmtree(out, ignore_errors=True)
        out.mkdir(parents=True)
        dest = out / f"pawpoller-move-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.zip"
        # Pause BEFORE the snapshot: anything posted while a big file is built would otherwise
        # still be "pending" in it, and the new PawPoller would post it a second time.
        if pause and not config.get_settings().get("moved_away_at"):
            _pause_this_copy()
            paused_now = True

        def _progress(done, total):
            _EXPORT.update(done_bytes=done, total_bytes=total)

        manifest = write_move_zip(dest, phrase, _progress)
        _EXPORT.update(state="ready", file=str(dest), logins=manifest["logins"],
                       size=dest.stat().st_size, files=manifest["files"], bytes=manifest["bytes"])
        logger.info("Move: moving file ready (%d files, %d bytes, logins %s).",
                    manifest["files"], dest.stat().st_size, manifest["logins"])
    except Exception as e:  # noqa: BLE001 — reported to the page, never raised into the thread
        shutil.rmtree(out, ignore_errors=True)
        if paused_now:
            resume_this_copy()            # no file, so no move: this copy carries on as it was
        _EXPORT.update(state="failed", error=f"Couldn't make the moving file ({type(e).__name__}).")
        logger.error("Move: making the moving file failed: %s", type(e).__name__)


@move_router.post("/export")
def export_start(body: dict):
    phrase = _check_phrase(body.get("phrase"))
    with _EXPORT_LOCK:
        if _EXPORT.get("state") == "building":
            raise HTTPException(409, detail="A moving file is already being made.")
        _EXPORT.clear()
        _EXPORT.update(state="building", done_bytes=0, total_bytes=0)
    threading.Thread(target=_run_export, args=(phrase, bool(body.get("pause", True))),
                     name="move-export", daemon=True).start()
    return {"state": "building"}


@move_router.get("/export")
def export_status():
    out = {k: v for k, v in _EXPORT.items() if k != "file"}
    out["moved_away_at"] = config.get_settings().get("moved_away_at")
    return out


@move_router.get("/export/file")
def export_file():
    """Repeatable and read-only (a lost download can be fetched again). The file is replaced by the
    next one made, or cleared after a day by prune_stale."""
    path = ready_file()
    if not path:
        raise HTTPException(404, detail="There's no moving file ready. Make one first.")
    return FileResponse(path, media_type="application/zip", filename=path.name)


def ready_file() -> Path | None:
    """The finished moving file, if there is one (also used by the desktop app's Save dialog)."""
    path = _EXPORT.get("file")
    if _EXPORT.get("state") != "ready" or not path or not Path(path).is_file():
        return None
    return Path(path)


@move_router.post("/resume")
def resume_this_copy():
    s = config.get_settings()
    back = s.get("moved_away_restore") or {}
    config.save_settings({"polling_paused": bool(back.get("polling_paused", False)),
                          "posting_enabled": bool(back.get("posting_enabled", False)),
                          "auto_sync_enabled": bool(back.get("auto_sync_enabled", False))})
    config.delete_settings_keys(["moved_away_at", "moved_away_restore"])
    logger.info("Move: this copy is working again.")
    return {"ok": True}


# ── Bringing it in (the new install) ─────────────────────────────────────────

_JSON_CAP = 16 * 1024 * 1024        # manifest, settings and sealed logins are small; refuse a bomb
_MAX_MEMBERS = 2_000_000
_UPLOAD_LOCK = threading.Lock()     # one piece at a time: two PUTs at one offset can't both append
_APPLY_LOCK = threading.Lock()      # check, apply, cancel and pruning never overlap
_APPLY: dict = {"state": "idle"}


def _in_dir() -> Path:
    return _data_dir() / "move-in"


def _upload_dir(uid: str) -> Path:
    if not _ID_RE.fullmatch(uid or ""):
        raise HTTPException(404, detail="No such upload.")
    d = _in_dir() / uid
    if not (d / "upload.json").is_file():
        raise HTTPException(404, detail="No such upload. Start again.")
    return d


def _status(d: Path) -> dict:
    meta = json.loads((d / "upload.json").read_text("utf-8"))
    part = d / "file.part"
    return {"id": meta["id"], "received": part.stat().st_size if part.is_file() else 0,
            "size": meta["size"], "name": meta["name"], "piece_bytes": PIECE_BYTES}


def _open_uploads() -> list[Path]:
    root = _in_dir()
    return [d for d in root.iterdir() if (d / "upload.json").is_file()] if root.is_dir() else []


def _room_needed(size: int) -> int:
    return int(size * 2.2) + 512 * 1024 * 1024    # the file, its unpacked contents, headroom


@move_router.post("/uploads")
def upload_open(body: dict):
    try:
        size = int(body.get("size"))
    except (TypeError, ValueError):
        raise HTTPException(400, detail="The file's size is missing.")
    name = str(body.get("name") or "")[:200]
    if size <= 0:
        raise HTTPException(400, detail="That file is empty.")
    for d in _open_uploads():
        st = _status(d)
        if st["name"] == name and st["size"] == size:
            return st                                   # the same file again: carry on
        raise HTTPException(409, detail={"message": "Another file is part-way in. Cancel it to start this one.",
                                         "id": st["id"], "name": st["name"]})
    free = shutil.disk_usage(_data_dir()).free
    if free < _room_needed(size):
        logger.warning("Move: refused an upload of %d bytes with %d free.", size, free)
        raise HTTPException(507, detail=f"There isn't room for this. It needs about "
                                        f"{_room_needed(size) / 1e9:.1f} GB free, and this has {free / 1e9:.1f} GB.")
    uid = secrets.token_hex(16)
    d = _in_dir() / uid
    d.mkdir(parents=True)
    (d / "upload.json").write_text(json.dumps({"id": uid, "name": name, "size": size,
                                               "created_at": time.time()}), "utf-8")
    (d / "file.part").touch()
    return _status(d)


@move_router.get("/uploads/{uid}")
def upload_status(uid: str):
    return _status(_upload_dir(uid))


@move_router.put("/uploads/{uid}")
async def upload_piece(uid: str, request: Request, offset: int):
    d = _upload_dir(uid)
    st = _status(d)
    if offset != st["received"]:
        raise HTTPException(409, detail={"message": "Out of step; carry on from here.", "received": st["received"]})
    room = min(PIECE_BYTES, st["size"] - st["received"])
    buf = bytearray()
    async for chunk in request.stream():              # never buffer more than a piece
        buf += chunk
        if len(buf) > room:
            raise HTTPException(413, detail="That piece is too big.")
    with _UPLOAD_LOCK:
        now = _status(d)["received"]
        if now != offset:                              # another PUT for this offset got here first
            raise HTTPException(409, detail={"message": "Out of step; carry on from here.", "received": now})
        with open(d / "file.part", "ab") as f:
            f.write(buf)
        os.utime(d / "upload.json")                    # "stale" counts from the last piece, not the first
    return {"received": offset + len(buf)}


@move_router.delete("/uploads/{uid}")
def upload_cancel(uid: str):
    d = _upload_dir(uid)
    _busy()
    try:
        direct = _is_direct(d)
        shutil.rmtree(d, ignore_errors=True)
        if direct:
            from routes import move_direct_api
            move_direct_api.forget_upload(uid)       # thrown away: its code is spent
    finally:
        _APPLY_LOCK.release()
    return {"ok": True}


def _is_direct(d: Path) -> bool:
    try:
        return bool(json.loads((d / "upload.json").read_text("utf-8")).get("direct"))
    except (OSError, ValueError):
        return False


def _phrase_for(d: Path, body: dict) -> str:
    """The phrase that opens an upload's logins: typed for a moving file, derived from the seat's code
    for one sent straight here (4.75.0, spec 036), so the body's phrase is ignored then."""
    if _is_direct(d):
        from routes import move_direct_api
        return move_direct_api.phrase_for_upload(json.loads((d / "upload.json").read_text("utf-8")))
    return str(body.get("phrase") or "")


def _member_ok(name: str) -> bool:
    if name in ("manifest.json", "logins.sealed", "data/pawpoller.db", "data/settings.json"):
        return True
    parts = name.split("/")
    return (len(parts) >= 3 and parts[0] == "data" and parts[1] in _DIRS and "\\" not in name and ":" not in name
            and all(p.rstrip(" .") not in ("", ".", "..") for p in parts[2:]))


def _small_json(z: zipfile.ZipFile, name: str):
    if z.getinfo(name).file_size > _JSON_CAP:
        raise ValueError(name)
    return json.loads(z.read(name))


_NOT_A_MOVE = "That isn't a PawPoller moving file. (An ordinary backup can't be brought in here.)"


def read_move(path: Path, phrase: str):
    """Check a moving file and open its logins. Returns (zipfile, manifest, creds); raises HTTPException.
    Reads at most a few small members into memory; everything else is checked by its declared size."""
    try:
        z = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError):
        raise HTTPException(400, detail="That isn't a PawPoller moving file (it isn't a zip, or it's damaged).")
    try:
        infos = z.infolist()
        if len(infos) > _MAX_MEMBERS:
            raise HTTPException(400, detail=_NOT_A_MOVE)
        try:
            manifest = _small_json(z, "manifest.json")
        except (KeyError, ValueError):
            raise HTTPException(400, detail=_NOT_A_MOVE)
        if not isinstance(manifest, dict) or manifest.get("kind") != MOVE_KIND or manifest.get("v") != 1:
            raise HTTPException(400, detail=_NOT_A_MOVE)
        names = {i.filename for i in infos}
        if "data/pawpoller.db" not in names or "data/settings.json" not in names:
            raise HTTPException(400, detail="That moving file is incomplete.")
        if any(not n.endswith("/") and not _member_ok(n) for n in names):
            raise HTTPException(400, detail="That moving file holds something it shouldn't, so it was refused.")
        files = {n for n in names if not n.endswith("/")}
        folders = {"/".join(n.split("/")[:k]) for n in files for k in range(3, len(n.split("/")))}
        if files & folders:
            raise HTTPException(400, detail="That moving file is damaged (a file and a folder share a name).")
        unpacked = sum(i.file_size for i in infos)
        if unpacked + 256 * 1024 * 1024 > shutil.disk_usage(_data_dir()).free:
            raise HTTPException(507, detail="There isn't room to unpack this moving file.")
        theirs, ours = str(manifest.get("app_version", "")), config.APP_VERSION
        if _version_tuple(theirs) > _version_tuple(ours):
            raise HTTPException(409, detail=f"This moving file was made by PawPoller {theirs}, and this one is "
                                            f"{ours}. Update this PawPoller first.")
        try:
            if not isinstance(_small_json(z, "data/settings.json"), dict):
                raise ValueError("settings")
        except (KeyError, ValueError):
            raise HTTPException(400, detail="That moving file is damaged.")
        creds = {}
        if manifest.get("logins") == "sealed":
            try:
                creds = open_sealed(_small_json(z, "logins.sealed"), phrase)
            except (KeyError, ValueError, WrongPhrase):
                raise HTTPException(403, detail="That phrase doesn't open this file.")
        return z, manifest, creds
    except BaseException:
        z.close()
        raise


def _here_has_work() -> bool:
    """Any tracked work already in this install? (US4: the person is warned before it is replaced.)"""
    conn = sqlite3.connect(str(config.DB_PATH))
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND (name LIKE '%submissions' OR name = 'masterpieces')")]
        for t in tables:
            if conn.execute('SELECT 1 FROM "%s" LIMIT 1' % t.replace('"', '""')).fetchone():   # MOVELOWS
                return True
    finally:
        conn.close()
    art = _dir_for("artwork")
    return art.is_dir() and any(p.is_file() for p in art.rglob("*"))


def _complete_upload(uid: str) -> Path:
    d = _upload_dir(uid)
    st = _status(d)
    if st["received"] != st["size"]:
        raise HTTPException(400, detail="The file hasn't finished uploading yet.")
    return d


def _busy() -> None:
    """Take the move lock or say a move is under way. The caller releases it."""
    if not _APPLY_LOCK.acquire(blocking=False):
        raise HTTPException(409, detail="A move is being brought in right now.")


def _int(v) -> int:
    try:
        return max(0, int(v))
    except (TypeError, ValueError):
        return 0


def _logins_word(manifest: dict) -> str:
    v = manifest.get("logins")
    return v if v in ("sealed", "none", "unreadable") else "none"


@move_router.post("/uploads/{uid}/check")
def upload_check(uid: str, body: dict):
    _busy()
    try:
        d = _complete_upload(uid)
        z, manifest, _creds = read_move(d / "file.part", _phrase_for(d, body))
        z.close()
    finally:
        _APPLY_LOCK.release()
    return {"app_version": str(manifest.get("app_version", "")), "logins": _logins_word(manifest),
            "files": _int(manifest.get("files")), "bytes": _int(manifest.get("bytes")),
            "here_has_work": _here_has_work()}


def _in_container() -> bool:
    return Path("/.dockerenv").exists()


def _restart_soon() -> None:
    def _go():
        time.sleep(3)
        logger.info("Move: restarting to finish loading the moved data.")
        os._exit(0)   # restart: unless-stopped brings the container straight back
    threading.Thread(target=_go, name="move-restart", daemon=True).start()


def _sqlite_copy(src_path: Path, dst_path: Path) -> None:
    src = sqlite3.connect(str(src_path))
    try:
        dst = sqlite3.connect(str(dst_path), timeout=60)   # wait out a poller's write, don't fail on it
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def _check_moved_db(path: Path) -> None:
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("damaged")
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' LIMIT 1").fetchone():
            raise ValueError("empty")
        theirs = conn.execute("PRAGMA page_size").fetchone()[0]
    finally:
        conn.close()
    live = sqlite3.connect(str(config.DB_PATH))
    try:
        ours = live.execute("PRAGMA page_size").fetchone()[0]
        mode = live.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        live.close()
    if theirs != ours and str(mode).lower() == "wal":
        raise ValueError("page size")


def _crc(path: Path) -> int:
    import zlib
    c = 0
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            c = zlib.crc32(block, c)
    return c


class MoveFailed(Exception):
    pass


def apply_move(z: zipfile.ZipFile, manifest: dict, creds: dict, work: Path) -> str:
    """Bring an opened moving file into this install. Returns the safety copy's folder name.

    Everything that can be checked is checked before anything live changes. Then: the database
    (one transaction), the settings (rolled back with the database if they fail), then media.
    A different file here at a moved file's name is kept in the safety copy, never lost.
    """
    if getattr(config, "_vault_unreadable", False):
        raise MoveFailed("This PawPoller can't read its own sign-in store right now, so nothing was changed.")
    # 1. Checks, on a private copy of the moved database.
    moved_db = work / "pawpoller.db"
    try:
        with z.open("data/pawpoller.db") as src, open(moved_db, "wb") as dst:
            shutil.copyfileobj(src, dst, 1024 * 1024)        # the CRC is verified as it's read
        _check_moved_db(moved_db)
    except (zipfile.BadZipFile, sqlite3.DatabaseError, ValueError):
        raise MoveFailed("The library inside the moving file is damaged, so nothing was changed.") from None
    moved = _small_json(z, "data/settings.json")
    moved.update(creds)
    here = config.get_settings()
    final = {k: v for k, v in moved.items() if not keeps_here(k)}
    final.update({k: v for k, v in here.items() if keeps_here(k)})
    # 4.77.3: the moved poll log is the old copy's history; rate watch counts from here on.
    final["moved_in_at"] = datetime.now(timezone.utc).isoformat()
    # A Trello board the old copy owned now belongs to this one (the old copy has stood down).
    trello = final.get("trello")
    if isinstance(trello, dict) and trello.get("owner_tag") and trello.get("owner_tag") == manifest.get("from_tag"):
        final["trello"] = {**trello, "owner_tag": str(here.get("trello_instance_tag") or "")}

    # 2. Safety copy.
    dd = _data_dir()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    safety = dd / f"restore-safety-{stamp}"
    safety.mkdir(parents=True, exist_ok=True)
    _sqlite_copy(Path(config.DB_PATH), safety / "pawpoller.db")
    for p in (config.SETTINGS_PATH, config.VAULT_PATH):
        if Path(p).is_file():
            shutil.copy2(p, safety / Path(p).name)

    # 3. Database, then settings; settings failing puts the database back.
    try:
        _sqlite_copy(moved_db, Path(config.DB_PATH))
    except sqlite3.Error:
        raise MoveFailed(f"The library couldn't be copied in, so nothing was changed. ({safety.name})") from None
    try:
        config.replace_settings(final)
    except Exception:
        _sqlite_copy(safety / "pawpoller.db", Path(config.DB_PATH))
        raise MoveFailed("The settings couldn't be saved, so the library was put back as it was.") from None
    _settle_queue()

    # 4. Media: added, never deleted. A different file already at a moved file's name is kept aside.
    roots = {name: _dir_for(name) for name in _DIRS}
    for info in z.infolist():
        parts = info.filename.split("/")
        if info.is_dir() or len(parts) < 3 or parts[0] != "data" or parts[1] not in roots:
            continue
        root = roots[parts[1]].resolve()
        rel = "/".join(parts[2:])
        target = (root / rel).resolve()
        if root not in target.parents:
            continue   # _member_ok already refused these; belt and braces
        if target.is_file():
            if target.stat().st_size == info.file_size and _crc(target) == info.CRC:
                continue                                  # the same file: nothing to do
            keep = safety / "media" / parts[1] / rel
            keep.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(target), str(keep))
        target.parent.mkdir(parents=True, exist_ok=True)
        with z.open(info) as src, open(target, "wb") as dst:
            shutil.copyfileobj(src, dst, 1024 * 1024)
    return safety.name


def _settle_queue() -> None:
    """A post the old copy was sending when it moved can't be finished here, and may have gone out.
    Mark it failed with a plain note instead of leaving it stuck (or sending it twice)."""
    conn = sqlite3.connect(str(config.DB_PATH), timeout=60)
    try:
        conn.execute("UPDATE posting_queue SET status = 'failed', completed_at = ?, last_error = ? "
                     "WHERE status = 'processing'",
                     (datetime.now(timezone.utc).isoformat(),
                      "Your old PawPoller was sending this when you moved. Check the site before trying again."))
        conn.commit()
    except sqlite3.OperationalError:
        pass                                              # no queue table in this library
    finally:
        conn.close()


def _run_apply(d: Path, phrase: str) -> None:
    try:
        z, manifest, creds = read_move(d / "file.part", phrase)
        try:
            work = d / "work"
            work.mkdir(exist_ok=True)
            safety = apply_move(z, manifest, creds, work)
        finally:
            z.close()
        direct = _is_direct(d)
        shutil.rmtree(d, ignore_errors=True)
        if direct:
            from routes import move_direct_api
            move_direct_api.forget_upload(d.name)        # brought in: its code is spent
        restarting = _in_container()
        _APPLY.update(state="done", safety_copy=safety, restarting=restarting, logins=_logins_word(manifest))
        logger.info("Move: brought in %d files (%d bytes), logins %s; safety copy %s.",
                    _int(manifest.get("files")), _int(manifest.get("bytes")), _logins_word(manifest), safety)
        if restarting:
            _restart_soon()
    except HTTPException as e:
        _APPLY.update(state="failed", error=e.detail if isinstance(e.detail, str) else "That file can't be brought in.")
    except MoveFailed as e:
        _APPLY.update(state="failed", error=str(e))
        logger.error("Move: bringing it in stopped: %s", e)
    except Exception as e:  # noqa: BLE001 — reported to the page, never raised into the thread
        _APPLY.update(state="failed", error=f"Bringing it in stopped ({type(e).__name__}). "
                                            "The safety copy in this PawPoller's data folder has what was here.")
        logger.error("Move: bringing it in failed: %s", type(e).__name__)
    finally:
        _APPLY_LOCK.release()


@move_router.post("/uploads/{uid}/apply")
def upload_apply(uid: str, body: dict):
    """Starts the move and answers at once (a big library takes longer than Cloudflare waits for one
    request); GET /api/move/apply says how it went."""
    if body.get("confirm") is not True:
        raise HTTPException(400, detail="Confirm first: this replaces what's in this PawPoller.")
    _busy()
    try:
        d = _complete_upload(uid)
        phrase = _phrase_for(d, body)
        z, _m, _c = read_move(d / "file.part", phrase)   # wrong phrase/file: say so now
        z.close()
        _APPLY.clear()
        _APPLY.update(state="applying")
        threading.Thread(target=_run_apply, args=(d, phrase),
                         name="move-apply", daemon=True).start()   # the thread releases the lock
    except BaseException:
        _APPLY_LOCK.release()
        raise
    return {"state": "applying"}


@move_router.get("/apply")
def apply_status():
    return dict(_APPLY)


def prune_stale(now: float | None = None) -> int:
    """Remove uploads and moving files untouched for a day. Called from the backup daemon's tick."""
    now = now or time.time()
    if not _APPLY_LOCK.acquire(blocking=False):
        return 0                                          # never under a move being brought in
    n = 0
    try:
        for root in (_in_dir(), _out_dir()):
            if not root.is_dir():
                continue
            for p in root.iterdir():
                try:
                    times = [p.stat().st_mtime] + ([c.stat().st_mtime for c in p.iterdir()] if p.is_dir() else [])
                    arrived = p.is_dir() and _arrived(p)
                    if now - max(times) > (ARRIVED_KEEP if arrived else STALE_SECONDS):
                        direct = p.is_dir() and _is_direct(p)
                        shutil.rmtree(p, ignore_errors=True) if p.is_dir() else p.unlink(missing_ok=True)
                        if direct:
                            from routes import move_direct_api
                            move_direct_api.forget_upload(p.name)
                        if root == _out_dir():
                            _EXPORT.clear()
                            _EXPORT.update(state="idle")
                        n += 1
                except OSError:
                    pass
    finally:
        _APPLY_LOCK.release()
    return n


def _arrived(d: Path) -> bool:
    try:
        return bool(json.loads((d / "upload.json").read_text("utf-8")).get("arrived_at"))
    except (OSError, ValueError):
        return False
