"""Mirroring primitives — manifests, safe extraction, database snapshots.

Stage 1 of ``docs/specs/desktop_server_mirroring.md``: the server is the source
of truth and the desktop receives a wholesale copy of the canonical stores
(artwork, ``posts_media``, the database). No FastAPI here on purpose — the
routes in ``routes/mirror_api.py`` are a thin shell over these functions so the
hard parts are testable without an app, a socket or a second machine.

Three decisions in here are not obvious, and each one is load-bearing.

**Why per-folder transfer instead of one tarball.** The artwork archive is
173 MB across 163 folders, but the largest single folder is 29 MB. The existing
sync endpoints build the whole archive in an ``io.BytesIO`` and read the whole
upload into memory with ``await file.read()``; doing that for the full archive
on the server means holding the source bytes and the gzip buffer at once on an
e2-micro that also runs Docker, Caddy and the app. Per-folder keeps the peak at
one folder, makes the transfer resumable after a dropped connection, and turns
the steady-state case into "fetch the handful of folders whose digest changed"
rather than a 173 MB re-download. The folder is also exactly the conflict unit
the spec identifies, because folder names are content-derived slugs.

**Why the database arrives as a pending file rather than being swapped live.**
SQLite cannot be replaced underneath a running application on Windows — the
open handle makes ``os.replace`` raise ``PermissionError`` — and even where it
is permitted, a reader mid-query would see a different database than it opened.
So a pull writes ``pawpoller.db.pending`` and ``apply_pending_snapshot()`` swaps
it in at startup, before ``init_db()`` opens anything. That makes the swap
atomic with respect to the app: it either happened before any connection
existed, or it did not happen. The cost is that the desktop must restart to see
mirrored data, which is worth stating plainly in the UI rather than engineering
around.

**Why ``session_cache`` never travels.** It holds a live Inkbunny session id.
Inkbunny binds a session to the IP that created it, so a server-created sid on
the desktop is at best dead weight; worse, both installs presenting the same sid
can invalidate each other, which shows up as the "login from this IP is not
permitted" failure rather than as anything resembling a sync bug. The vault is
a non-issue by comparison — credentials live in ``settings.vault.json``, not in
the database, so the database carries nothing that needs the ``.vault_key`` that
deliberately never crosses.
"""
from __future__ import annotations

import hashlib
import io
import logging
import re
import shutil
import sqlite3
import ntpath as _ntpath
import posixpath as _posixpath
import tarfile
import time
from pathlib import Path
from typing import Iterable, Iterator

logger = logging.getLogger(__name__)

# Per §4 of the spec these are DERIVED — per-device undo history, timestamp
# named and pruned to 10, so mirroring them churns every folder's digest for no
# benefit. `.bak.<unix-ts>` is written by the artwork writer and the artist
# migration alike.
_BAK_RE = re.compile(r"\.bak\.\d+$")

# Tables emptied out of a snapshot before it leaves the server. Keep this list
# short and justified — anything dropped here is data the desktop then lacks.
SNAPSHOT_EXCLUDE_TABLES = ("session_cache",)

PENDING_DB_SUFFIX = ".pending"


class MirrorSecurityError(Exception):
    """A tar member tried to escape its destination directory."""


# ── Path filtering ────────────────────────────────────────────

def is_mirrored_file(path: Path, root: Path) -> bool:
    """True if `path` is canonical content rather than derived or per-device.

    Excludes dotfiles at any depth (``.vault_key`` must never cross, and dot
    directories are per-device state) and ``*.bak.<ts>`` undo history.
    """
    try:
        rel = path.relative_to(root)
    except ValueError:
        return False
    for part in rel.parts:
        if part.startswith("."):
            return False
    return not _BAK_RE.search(path.name)


def is_mirrored_story_file(path: Path, root: Path) -> bool:
    """True if `path` is canonical STORY content (3.19.0).

    Delegates to `deploy/archive_sync_rules.is_excluded`, which is already the
    one place the archive's exclusions live — that module exists precisely
    because `pawsync` and `pawpull` each carried their own list and the two
    drifted, so a push-then-pull did not return the archive to where it
    started. Adding a THIRD list here for the mirror would recreate that bug
    with an extra participant.

    The artwork rule (`is_mirrored_file`) is not reusable: the story archive
    legitimately carries derived-but-needed trees — `HTML/`, `BBCode/`, `PDF/`,
    `EPUB/`, `Chapters/` — because the posters upload those files directly and
    the server has no browser to regenerate a PDF. Dotfiles are still refused
    here, since `.vault_key` must never cross whatever store it is in.
    """
    try:
        rel = path.relative_to(root)
    except ValueError:
        return False
    for part in rel.parts:
        if part.startswith("."):
            return False
    try:
        from deploy.archive_sync_rules import is_excluded
    except Exception:  # noqa: BLE001 — rules module absent: fall back to strict
        return is_mirrored_file(path, root)
    return not is_excluded(rel.as_posix())


def iter_mirrored_files(folder: Path, root: Path | None = None,
                        include=None) -> Iterator[Path]:
    """Yield every mirrorable file under `folder`, in a stable sorted order.

    Sorted because the folder digest is order-sensitive: two installs must
    derive the same digest from the same bytes, and filesystem iteration order
    is not guaranteed to match between NTFS and ext4.
    """
    base = root if root is not None else folder
    predicate = include or is_mirrored_file
    for path in sorted(folder.rglob("*"), key=lambda p: p.as_posix()):
        if path.is_file() and predicate(path, base):
            yield path


# ── Digests ───────────────────────────────────────────────────

def file_sha256(path: Path, chunk_size: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(chunk_size), b""):
            h.update(block)
    return h.hexdigest()


def folder_manifest(folder: Path, *, detail: bool = False, include=None) -> dict:
    """Describe one folder: size, file count and a content digest.

    The digest covers the relative path, size and content hash of every
    mirrorable file. Content alone would miss a rename; paths alone would miss
    an edit. `mtime` is deliberately absent — tar extraction rewrites it, so a
    freshly pulled folder would otherwise never match the source it came from.
    """
    files: list[dict] = []
    total = 0
    count = 0
    h = hashlib.sha256()
    for path in iter_mirrored_files(folder, include=include):
        rel = path.relative_to(folder).as_posix()
        size = path.stat().st_size
        digest = file_sha256(path)
        total += size
        count += 1
        h.update(rel.encode("utf-8"))
        h.update(str(size).encode("ascii"))
        h.update(digest.encode("ascii"))
        if detail:
            files.append({"path": rel, "size": size, "sha256": digest})
    entry = {
        "name": folder.name,
        "file_count": count,
        "bytes": total,
        "digest": h.hexdigest(),
    }
    if detail:
        entry["files"] = files
    return entry


def build_manifest(root: Path, *, detail: bool = False, include=None) -> dict:
    """Manifest of a store laid out as one directory per item (the artwork archive)."""
    if not root.is_dir():
        return {"root": str(root), "exists": False, "folders": [], "bytes": 0, "count": 0}
    folders = [
        folder_manifest(entry, detail=detail, include=include)
        for entry in sorted(root.iterdir(), key=lambda p: p.name)
        if entry.is_dir() and not entry.name.startswith(".")
    ]
    return {
        "root": str(root),
        "exists": True,
        "folders": folders,
        "count": len(folders),
        "bytes": sum(f["bytes"] for f in folders),
    }


def build_flat_manifest(root: Path, *, detail: bool = False) -> dict:
    """Manifest of a store that is a flat pile of files (``posts_media``).

    ⚠ ``posts_media`` filenames are ``{post_id}_{idx}{ext}`` — DB surrogate
    keys (spec §4). They are only safe to mirror because the database travels
    with them in the same pull, so the ids on both sides refer to the same
    posts. If the database ever stops travelling, this store must be re-keyed
    before it moves.
    """
    if not root.is_dir():
        return {"root": str(root), "exists": False, "files": [], "bytes": 0, "count": 0}
    files = []
    total = 0
    h = hashlib.sha256()
    for path in iter_mirrored_files(root):
        rel = path.relative_to(root).as_posix()
        size = path.stat().st_size
        digest = file_sha256(path)
        total += size
        h.update(rel.encode("utf-8"))
        h.update(str(size).encode("ascii"))
        h.update(digest.encode("ascii"))
        files.append({"path": rel, "size": size, "sha256": digest} if detail else {"path": rel})
    return {
        "root": str(root),
        "exists": True,
        "files": files,
        "count": len(files),
        "bytes": total,
        "digest": h.hexdigest(),
    }


# ── Tar packing / extraction ──────────────────────────────────

def pack_folder(folder: Path, arcname: str | None = None, include=None) -> bytes:
    """Tar+gzip one folder into memory, excluding derived and dotted files."""
    root = folder
    name = arcname or folder.name
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for path in iter_mirrored_files(folder, root, include=include):
            tar.add(str(path), arcname=f"{name}/{path.relative_to(root).as_posix()}")
    return buf.getvalue()


def pack_folder_files(folder: Path, paths: list[str], arcname: str | None = None,
                      include=None) -> bytes:
    """Tar+gzip only `paths` (folder-relative, posix) from one folder.

    The per-file half of the 3.18.0 fetch. `folder_manifest(detail=True)` has
    always reported a sha256 per file and the puller has always requested it,
    but the only way to GET anything was the whole folder — so one edited
    `masterpiece.json` re-downloaded the 29 MB image beside it. Measured on the
    live pair: 158.6 MB moved to deliver 0.2 MB of change.

    ⚠ Each requested path is validated as if it had arrived inside an archive,
    with the SAME helpers the extractor uses — `_reject_foreign_absolute` plus a
    containment re-check on the resolved target. A request parameter that names
    a file is exactly the shape traversal arrives in, and giving it its own
    private check is the mistake 3.17.4 had to undo in three places.
    """
    root = folder.resolve()
    name = arcname or folder.name
    # ⚠ Dedupe and budget, or this is a memory-exhaustion primitive.
    # The route caps the LIST at 5000 entries, but nothing stopped the same
    # 29 MB image being named 5000 times: each is tar-added separately, artwork
    # is already-compressed PNG/JPEG so gzip reclaims nothing, and the whole
    # archive accumulates in a BytesIO before `Response(content=...)` doubles
    # the peak. Measured: 200 copies of a 2 MB file produced a 400 MB payload,
    # linear with no dedup — on a 1 GB e2-micro that also runs Docker and
    # Caddy, one request is enough.
    #
    # The budget is the folder's own size: a caller can never legitimately need
    # MORE bytes than the folder contains, and that ceiling preserves the
    # invariant this module's docstring sets — peak stays at one folder.
    seen: set[str] = set()
    budget = sum(p.stat().st_size for p in iter_mirrored_files(root, root, include=include))
    used = 0

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for rel in paths:
            _reject_foreign_absolute(rel)
            if ".." in Path(rel).parts:
                raise MirrorSecurityError(f"Unsafe path requested: {rel}")
            target = (root / rel).resolve()
            if root not in target.parents:
                raise MirrorSecurityError(f"Path escapes the folder: {rel}")
            # Absent is not an error: the server may have deleted a file since
            # the manifest was taken, and a partial fetch is still progress.
            # It is also not silent — the caller compares what it asked for
            # against what arrived.
            if not target.is_file() or not (include or is_mirrored_file)(target, root):
                continue
            # Keyed on the RESOLVED target, not the requested string, so two
            # spellings of one file (case differences on Windows, `./x` vs `x`)
            # collapse to a single member.
            key = str(target).lower()
            if key in seen:
                continue
            seen.add(key)
            used += target.stat().st_size
            if used > budget:
                raise MirrorSecurityError(
                    "Requested files exceed the folder's own size")
            tar.add(str(target), arcname=f"{name}/{Path(rel).as_posix()}")
    return buf.getvalue()


def pack_files(root: Path) -> bytes:
    """Tar+gzip a flat directory of files into memory."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for path in iter_mirrored_files(root, root):
            tar.add(str(path), arcname=path.relative_to(root).as_posix())
    return buf.getvalue()


_DRIVE_RE = re.compile(r"^[A-Za-z]:")


def _reject_foreign_absolute(name: str) -> None:
    """Refuse a member name that is absolute under EITHER path convention.

    tar's format specifies `/` as its separator, so a backslash in a member
    name is never a directory separator the archive is entitled to — it is
    either a Windows path smuggled in, or a filename unusual enough that
    refusing it costs nothing. Drive letters are rejected too, including the
    drive-RELATIVE form (`C:evil`), which `ntpath.isabs` reports as False
    while Windows still resolves it against that drive's working directory.
    """
    # A NUL is reachable: tarfile's WRITER truncates at one, but its READER
    # hands back the raw value from a crafted PAX header. `Path.resolve()` then
    # raises ValueError("embedded null character") — which `mirror_api`'s
    # except tuple does not catch, so ONE bad member aborted an entire pull
    # instead of skipping one folder. Rejected here, where it is a member
    # problem rather than a crash.
    if chr(0) in name:
        raise MirrorSecurityError("NUL in archive member name")
    if _ntpath.isabs(name) or _posixpath.isabs(name):
        raise MirrorSecurityError(f"Absolute path in archive: {name}")
    # ⚠ The backslash rule is LOAD-BEARING, not belt-and-braces. Two cases
    # where `ntpath.isabs` alone returns False and only this clause saves them:
    #   \server\share  — splitdrive consumes it all, leaving rest == ""
    #   oo            — True on Python ≤ 3.12, FALSE on 3.13+ (gh-44626
    #                     stopped treating one leading separator as absolute)
    # The image is python:3.11-slim today, so the second is dormant — but
    # relaxing this rule to permit backslashes in filenames would silently
    # reopen it on a 3.13 bump. Do not relax it casually.
    if chr(92) in name or _DRIVE_RE.match(name):
        raise MirrorSecurityError(f"Windows path in archive: {name}")


def safe_extract(tar: tarfile.TarFile, dest: Path) -> None:
    """Extract `tar` into `dest`, refusing anything that could escape it.

    This is ``posting_api.sync_upload``'s hardening (spec D3 names it as the
    pattern to copy): link members are rejected outright, because a symlink
    whose *own* name is innocuous can still redirect a later member's write
    outside the tree, and every remaining member's **resolved** target must sit
    under the destination. A substring check for ``".."`` is not equivalent —
    it misses absolute paths on Windows (``C:\\…``), drive-relative paths, and
    anything reached through a link already on disk.
    """
    base = dest.resolve()
    for member in tar.getmembers():
        if member.issym() or member.islnk():
            raise MirrorSecurityError(f"Link members not allowed in archive: {member.name}")
        if member.isdev():
            raise MirrorSecurityError(f"Device members not allowed in archive: {member.name}")
        # ⚠ resolve() alone is PLATFORM-DEPENDENT and does not deliver the
        # promise made above (3.17.4). On Windows `C:\Windows\evil.dll` is
        # absolute and was rejected; on Linux it is an ordinary filename
        # containing backslashes, so it sailed through — and the server is
        # Linux. The test that proved the Windows behaviour was written on
        # Windows, so it passed locally and went red only in CI.
        #
        # A tar crossing a Windows<->Linux mirror has to be judged by BOTH
        # conventions wherever the check runs: what is a harmless filename on
        # the receiving box can be an absolute path on the box it came from,
        # or on the next one to unpack it.
        _reject_foreign_absolute(member.name)
        target = (base / member.name).resolve()
        if target != base and base not in target.parents:
            raise MirrorSecurityError(f"Unsafe path in archive: {member.name}")
    tar.extractall(path=str(base))


def extract_bytes(payload: bytes, dest: Path) -> list[str]:
    """Safely extract a .tar.gz held in memory; returns the member names."""
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as tar:
        names = [m.name for m in tar.getmembers()]
        safe_extract(tar, dest)
    return names


# ── Database snapshots ────────────────────────────────────────

def snapshot_database(src: Path, dest: Path,
                      exclude_tables: Iterable[str] = SNAPSHOT_EXCLUDE_TABLES) -> dict:
    """Write a transactionally consistent copy of `src` to `dest`.

    Uses ``Connection.backup()`` rather than copying the file. Connections run
    in WAL (``db.py:74``), so committed rows can still live in
    ``pawpoller.db-wal`` at any instant — a file copy of the ``.db`` alone
    yields a database silently missing its most recent commits. ``backup()``
    reads through the WAL and is safe against concurrent writers.

    (``backup_api.write_backup_zip`` still has the file-copy bug this avoids;
    it is filed rather than fixed here — see the spec §5.1.)
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()

    src_conn = sqlite3.connect(str(src), timeout=30)
    try:
        dst_conn = sqlite3.connect(str(dest), timeout=30)
        try:
            src_conn.backup(dst_conn)
            removed = {}
            for table in exclude_tables:
                try:
                    cur = dst_conn.execute(f'DELETE FROM "{table}"')
                    removed[table] = cur.rowcount
                except sqlite3.OperationalError:
                    # Table absent on this schema version — nothing to strip.
                    removed[table] = 0
            dst_conn.commit()
            # VACUUM after the deletes so the excluded rows are not merely
            # unlinked but actually absent from the bytes that travel.
            dst_conn.execute("VACUUM")
            tables = [r[0] for r in dst_conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")]
            # backup() inherits the source's journal mode, so the snapshot would
            # be a WAL database — and a WAL database is THREE files. Only the
            # .db travels and only the .db gets swapped in, so anything sitting
            # in a sidecar would be silently dropped. DELETE mode makes the
            # artifact self-contained. The receiving install is unaffected:
            # get_connection() sets journal_mode=WAL on every connection, so it
            # flips back the moment the database is actually used.
            dst_conn.execute("PRAGMA journal_mode=DELETE")
        finally:
            dst_conn.close()
    finally:
        src_conn.close()

    return {
        "path": str(dest),
        "bytes": dest.stat().st_size,
        "tables": len(tables),
        "excluded": removed,
    }


def verify_snapshot(path: Path) -> dict:
    """Integrity-check a received snapshot before it is allowed to replace anything.

    A truncated download is the expected failure here, and a truncated SQLite
    file often opens fine and only fails on the page that is missing — so this
    runs ``integrity_check`` rather than settling for "it opened".
    """
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    try:
        result = conn.execute("PRAGMA integrity_check").fetchone()[0]
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
        accounts = 0
        if "accounts" in tables:
            accounts = conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
    finally:
        conn.close()
    return {"ok": result == "ok", "integrity": result,
            "tables": len(tables), "accounts": accounts}


def pending_snapshot_path(db_path: Path) -> Path:
    return db_path.with_name(db_path.name + PENDING_DB_SUFFIX)


def discard_pending_snapshot(db_path: Path) -> None:
    """Remove the pending slot *and its sidecars*.

    Always all three. Overwriting just the ``.db`` leaves the previous
    attempt's ``-wal``/``-shm`` beside a database they do not belong to, and
    SQLite will happily try to recover one database's write-ahead log into
    another. Observed for real: after re-staging, the sidecars on disk were
    516 seconds older than the file they sat next to.
    """
    pending = pending_snapshot_path(db_path)
    for suffix in ("", "-wal", "-shm"):
        pending.with_name(pending.name + suffix).unlink(missing_ok=True)


def stage_pending_snapshot(db_path: Path, payload: bytes) -> dict:
    """Write a downloaded snapshot into the pending slot and verify it.

    The single place a pending file is created, so the sidecar clearing above
    cannot be forgotten by a caller. A snapshot that fails verification is
    discarded here rather than left to be rejected later at startup — a bad
    file sitting in the slot is a trap for the next person reading the folder.
    """
    pending = pending_snapshot_path(db_path)
    discard_pending_snapshot(db_path)
    pending.parent.mkdir(parents=True, exist_ok=True)
    pending.write_bytes(payload)
    try:
        check = verify_snapshot(pending)
    except sqlite3.Error as e:
        discard_pending_snapshot(db_path)
        return {"ok": False, "integrity": f"unreadable: {e}", "tables": 0, "accounts": 0}
    if not check["ok"]:
        discard_pending_snapshot(db_path)
    return check


def apply_pending_snapshot(db_path: Path) -> dict | None:
    """Swap in a pulled snapshot, if one is waiting. Call before opening the DB.

    Returns None when there is nothing to do, which is the overwhelmingly
    common case, so this stays cheap enough to run on every startup.

    The current database is renamed aside to ``pawpoller.db.bak.<ts>`` rather
    than deleted. That is not politeness: the desktop's own history (5,625 IB
    snapshots at the time of writing) is not represented anywhere else, and the
    project's standing rule is that nothing is destroyed when hiding or
    setting it aside will do. A refused swap must leave the install exactly as
    it was, so the snapshot is verified *first* and a failed verification
    leaves the live database untouched.
    """
    pending = pending_snapshot_path(db_path)
    if not pending.exists():
        return None

    try:
        check = verify_snapshot(pending)
    except sqlite3.Error as e:
        logger.error("Pending snapshot is unreadable (%s) — discarding, live DB untouched", e)
        discard_pending_snapshot(db_path)
        return {"applied": False, "reason": f"unreadable: {e}"}

    if not check["ok"]:
        logger.error("Pending snapshot failed integrity check (%s) — discarding, live DB untouched",
                     check["integrity"])
        discard_pending_snapshot(db_path)
        return {"applied": False, "reason": f"integrity: {check['integrity']}"}

    # A snapshot written by a current server arrives in DELETE mode and has no
    # sidecars, but one staged by an older build (or opened by another tool)
    # can. Fold them into the .db before it moves, or the swap drops whatever
    # they hold — the same mistake this function guards against on the way out.
    if pending.with_name(pending.name + "-wal").exists():
        try:
            conn = sqlite3.connect(str(pending), timeout=30)
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                conn.execute("PRAGMA journal_mode=DELETE")
            finally:
                conn.close()
        except sqlite3.Error as e:
            logger.error("Pending snapshot could not be checkpointed (%s) — "
                         "discarding rather than risk a partial swap", e)
            discard_pending_snapshot(db_path)
            return {"applied": False, "reason": f"checkpoint failed: {e}"}

    backup = None
    if db_path.exists():
        backup = db_path.with_name(f"{db_path.name}.bak.{int(time.time())}")
        shutil.move(str(db_path), str(backup))
        # WAL/SHM belong to the database we just moved aside. Leaving them next
        # to the new file would have SQLite try to recover another database's
        # write-ahead log into it.
        for side in ("-wal", "-shm"):
            stale = db_path.with_name(db_path.name + side)
            if stale.exists():
                stale.unlink()

    shutil.move(str(pending), str(db_path))
    for suffix in ("-wal", "-shm"):
        pending.with_name(pending.name + suffix).unlink(missing_ok=True)
    logger.warning("Mirror: applied pulled database snapshot (%d tables, %d accounts); "
                   "previous database preserved at %s",
                   check["tables"], check["accounts"], backup.name if backup else "(none)")
    return {
        "applied": True,
        "tables": check["tables"],
        "accounts": check["accounts"],
        "previous_database": str(backup) if backup else None,
    }


# ── Diffing ───────────────────────────────────────────────────

def diff_manifests(remote: dict, local: dict) -> dict:
    """Work out which folders to fetch. Never proposes a deletion.

    Folders present locally but not remotely are reported as ``local_only`` and
    left alone. The project rule is that art is never deleted — and beyond the
    rule, a folder missing from the server is equally consistent with "removed
    there" and "not yet pushed from here", and this direction of sync has no
    way to tell those apart.

    **Containment, not equality.** A folder needs fetching when the server has
    a file the local copy lacks or differs on — *not* when the two digests
    disagree. Those are different questions the moment the local folder holds a
    file the server does not, which is the normal state here rather than an
    edge case: a mirror that never deletes accumulates exactly such files.
    ``Ms_Kristoff`` is the live example — it carries a legacy ``artwork.json``
    from before the ``masterpiece.json`` rename, so under digest equality it
    reported "changed" after a successful pull and would have re-downloaded on
    every run forever while looking like it was working. This is the same trap
    as including ``mtime`` in the digest, arriving from a different direction.

    The digest is still the fast path: equal digests mean identical content and
    skip the per-file walk. Only a mismatch pays for the detailed comparison,
    and only when both sides carry per-file detail — without it there is no way
    to tell surplus from divergence, so the safe answer is to fetch.
    """
    remote_by_name = {f["name"]: f for f in remote.get("folders", [])}
    local_by_name = {f["name"]: f for f in local.get("folders", [])}

    missing, changed, unchanged = [], [], []
    extra_local: dict[str, list[str]] = {}
    # Which FILES a changed folder actually needs (3.18.0). Populated only when
    # both sides carry per-file detail; a folder absent from this map has to be
    # fetched whole, which is also the right answer for a missing folder.
    changed_files: dict[str, list[str]] = {}
    changed_file_bytes = 0

    for name, entry in remote_by_name.items():
        local_entry = local_by_name.get(name)
        if local_entry is None:
            missing.append(name)
            continue
        if local_entry["digest"] == entry["digest"]:
            unchanged.append(name)
            continue

        remote_files = {f["path"]: f.get("sha256") for f in entry.get("files", [])}
        local_files = {f["path"]: f.get("sha256") for f in local_entry.get("files", [])}
        if not remote_files or not local_files:
            changed.append(name)
            continue

        stale = sorted(path for path, digest in remote_files.items()
                       if local_files.get(path) != digest)
        if stale:
            changed.append(name)
            changed_files[name] = stale
            sizes = {f["path"]: f.get("size", 0) for f in entry.get("files", [])}
            changed_file_bytes += sum(sizes.get(p, 0) for p in stale)
        else:
            unchanged.append(name)
            surplus = sorted(set(local_files) - set(remote_files))
            if surplus:
                extra_local[name] = surplus

    local_only = sorted(set(local_by_name) - set(remote_by_name))
    fetch = sorted(missing + changed)
    # A missing folder has no per-file plan and is fetched whole.
    missing_bytes = sum(remote_by_name[n]["bytes"] for n in missing)
    return {
        "fetch": fetch,
        "missing": sorted(missing),
        "changed": sorted(changed),
        "unchanged": sorted(unchanged),
        "local_only": local_only,
        "extra_local_files": extra_local,
        # What a whole-folder fetch would move — kept because it is the honest
        # "before" number and the one to compare against.
        "fetch_bytes": sum(remote_by_name[n]["bytes"] for n in fetch),
        # What a per-file fetch actually moves. On the measured case that made
        # this necessary these differed by 839x: 158.6 MB of folders carrying
        # 0.2 MB of changed files, because 152 works had one edited
        # `masterpiece.json` sitting beside an untouched 29 MB image.
        "changed_files": changed_files,
        "fetch_file_bytes": missing_bytes + changed_file_bytes,
    }
