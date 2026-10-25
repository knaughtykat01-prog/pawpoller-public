"""A PostyBirb folder uploaded to a server or seat (spec 060 US2).

A server can't read the person's disk, so they zip PostyBirb's folder (and, for logins,
its Partitions folder) and send it in pieces. The zip holds live sign-ins for every site
PostyBirb knows, so:

- only the signed-in owner in a browser may send one (a paired install's API key is refused
  by the router), and only over https, from the machine itself, or over Tailscale (FR-SEC-005);
- ``take()`` copies out only what the import reads (the ``.db`` files, each partition's
  ``Cookies``, and the files under ``submission/``), with every size counted as it is written,
  never trusted from the zip's own listing (FR-SEC-002);
- the session folder is deleted when the import ends, is cancelled, a new session starts,
  or 30 minutes after the last call; and on start-up (FR-SEC-008). One lock covers import,
  delete and sweep, so nothing pulls files out from under a running import.

Layout after ``take()``: ``<session>/x/docs`` (the PostyBirb folder) and ``<session>/x/app``
(``Partitions``), which is what ``pb_import._dirs(base)`` reads.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import re
import secrets
import shutil
import threading
import time
import zipfile
from pathlib import Path

import config

logger = logging.getLogger(__name__)

PIECE_BYTES = 32 * 1024 * 1024          # each request stays under Cloudflare's 100 MB
MAX_UPLOAD = 2 * 1024 ** 3              # the session's zips together
MAX_FILES = 2
MAX_MEMBERS = 5_000                     # entries taken out (the zip itself may list more; they're skipped)
MAX_MEMBER = 512 * 1024 ** 2
MAX_TOTAL = 4 * 1024 ** 3
MAX_RATIO = 100
RATIO_FLOOR = 1024 * 1024             # small files (a sparse Cookies db) may pack tighter
IDLE_SECS = 30 * 60
HEADROOM = 1024 ** 3

_ID = re.compile(r"^[0-9a-f]{32}$")
_FILE = re.compile(r"^[12]$")
_TAILSCALE = (ipaddress.ip_network("100.64.0.0/10"), ipaddress.ip_network("fd7a:115c:a1e0::/48"))
LOCK = threading.Lock()                 # import, delete, new session and sweep never overlap
_PIECE_LOCK = threading.Lock()

# What an import reads, by its path inside the PostyBirb folder (after the prefix is found).
_DOCS_MEMBER = re.compile(r"^(data/[A-Za-z0-9_-]{1,64}\.db|submission/(files|thumbnails)/[^/]{1,255})$")
_APP_MEMBER = re.compile(r"^Partitions/[A-Za-z0-9_-]{1,64}/(Network/)?Cookies$")


class Refused(Exception):
    """A plain-words reason the upload can't be used (a 400 at the route)."""


def root() -> Path:
    return Path(config.DATA_DIR) / "pb_upload"


def secure(scheme: str, peer: str) -> bool:
    """https, the machine itself, or a Tailscale peer. *scheme* and *peer* are uvicorn's view,
    which honours proxy headers only from PAWPOLLER_FORWARDED_IPS (FR-SEC-005)."""
    if scheme == "https":
        return True
    try:
        ip = ipaddress.ip_address(peer)
    except ValueError:
        return peer == "localhost"
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_loopback or any(ip in n for n in _TAILSCALE if n.version == ip.version)


def _dir(uid: str) -> Path:
    if not _ID.fullmatch(uid or ""):
        raise KeyError(uid)
    d = root() / uid
    meta = d / "session.json"
    if not meta.is_file():
        raise KeyError(uid)
    if time.time() - meta.stat().st_mtime > IDLE_SECS and not LOCK.locked():
        _wipe(d)                                  # idle 30 minutes: gone, whenever the sweep next runs
        raise KeyError(uid)
    return d


def _meta(d: Path) -> dict:
    return json.loads((d / "session.json").read_text("utf-8"))


def _touch(d: Path) -> None:
    (d / "session.json").touch()


def _wipe(d: Path) -> None:
    shutil.rmtree(d, ignore_errors=True)


def busy() -> bool:
    return LOCK.locked()


def start() -> str:
    """A new session; any older one is deleted. Caller holds LOCK."""
    r = root()
    if r.is_dir():
        for old in r.iterdir():
            _wipe(old)
    uid = secrets.token_hex(16)
    d = r / uid
    d.mkdir(parents=True, mode=0o700)
    (d / "session.json").write_text(json.dumps({"files": {}, "ready": False, "created": time.time()}), "utf-8")
    return uid


def add_file(uid: str, size: int) -> dict:
    d = _dir(uid)
    meta = _meta(d)
    if len(meta["files"]) >= MAX_FILES:
        raise Refused("Two zips at most: the PostyBirb folder, and its logins folder if that is separate.")
    if size <= 0:
        raise Refused("That file is empty.")
    if sum(meta["files"].values()) + size > MAX_UPLOAD:
        raise OverflowError()
    if shutil.disk_usage(root()).free < sum(meta["files"].values()) + size + MAX_TOTAL // 4 + HEADROOM:
        raise Refused("There isn't room on this server for that upload.")
    fid = str(len(meta["files"]) + 1)
    meta["files"][fid] = size
    (d / f"{fid}.zip").touch()
    (d / "session.json").write_text(json.dumps(meta), "utf-8")
    return {"file": fid, "piece_bytes": PIECE_BYTES, "received": 0}


def received(uid: str, fid: str) -> int:
    d = _dir(uid)
    if not _FILE.fullmatch(fid or "") or fid not in _meta(d)["files"]:
        raise KeyError(fid)
    return (d / f"{fid}.zip").stat().st_size


def write_piece(uid: str, fid: str, offset: int, data: bytes) -> int:
    """Append one piece at *offset*. Returns the new count; ValueError(count) when out of step."""
    d = _dir(uid)
    size = _meta(d)["files"].get(fid)
    if size is None or not _FILE.fullmatch(fid):
        raise KeyError(fid)
    with _PIECE_LOCK:
        now = (d / f"{fid}.zip").stat().st_size
        if offset != now:
            raise ValueError(now)
        if now + len(data) > size or len(data) > PIECE_BYTES:
            raise OverflowError()
        with open(d / f"{fid}.zip", "ab") as f:
            f.write(data)
        _touch(d)
        return now + len(data)


def _prefix(names: list[str], marker: str) -> str | None:
    """The folder inside the zip that holds *marker* (shortest wins), e.g. 'PostyBirb/'."""
    best = None
    for n in names:
        if n == marker or n.endswith("/" + marker):
            p = n[: len(n) - len(marker)]
            if best is None or len(p) < len(best):
                best = p
    return best


def _app_prefix(names: list[str]) -> str | None:
    """The folder inside the zip that holds ``Partitions/`` (shortest wins), e.g. 'postybirb-plus/'."""
    best = None
    for n in names:
        i = 0 if n.startswith("Partitions/") else (n.find("/Partitions/") + 1 if "/Partitions/" in n else -1)
        if i >= 0 and (best is None or i < len(best)):
            best = n[:i]
    return best


def _copy_capped(src, dst: Path, limit: int) -> int:
    n = 0
    with open(dst, "wb") as out:
        while True:
            chunk = src.read(1 << 20)
            if not chunk:
                return n
            n += len(chunk)
            if n > limit:
                raise Refused("A file in that zip is bigger than it says, or too big to bring in.")
            out.write(chunk)


def take(uid: str) -> dict:
    """Unpack what the import reads from every zip in the session (FR-SEC-002). Caller holds LOCK.
    Any refusal removes the whole session folder."""
    d = _dir(uid)
    meta = _meta(d)
    dest = d / "x"
    try:
        for fid, size in meta["files"].items():
            if (d / f"{fid}.zip").stat().st_size != size:
                raise Refused("That upload didn't finish. Send it again.")
        total, count, has = 0, 0, {"submissions": False, "logins": False}
        for fid in meta["files"]:
            try:
                z = zipfile.ZipFile(d / f"{fid}.zip")
            except zipfile.BadZipFile:
                raise Refused("That isn't a zip file.")
            with z:
                infos = [i for i in z.infolist() if not i.is_dir()]
                names = [i.filename.replace("\\", "/") for i in infos]
                docs_pre = _prefix(names, "data/submissions.db")
                if docs_pre is None:
                    docs_pre = _prefix(names, "data/accounts.db")
                app_pre = _app_prefix(names)
                for info, name in zip(infos, names):
                    if ".." in name.split("/") or name.startswith("/") or ":" in name.split("/")[0]:
                        continue
                    target = None
                    if docs_pre is not None and name.startswith(docs_pre) and _DOCS_MEMBER.fullmatch(name[len(docs_pre):]):
                        target = dest / "docs" / name[len(docs_pre):]
                    elif app_pre is not None and name.startswith(app_pre) and _APP_MEMBER.fullmatch(name[len(app_pre):]):
                        target = dest / "app" / name[len(app_pre):]
                    if target is None:
                        continue                       # Local Storage, caches, anything else: never written
                    count += 1
                    if count > MAX_MEMBERS:
                        raise Refused("That zip has too many files in it.")
                    packed_tight = info.file_size > RATIO_FLOOR and info.file_size / max(info.compress_size, 1) > MAX_RATIO
                    if info.file_size > MAX_MEMBER or packed_tight:
                        raise Refused("A file in that zip is too big, or packed too tightly to be safe.")
                    if total + info.file_size > MAX_TOTAL:
                        raise Refused("That zip unpacks to more than 4 GB.")
                    if shutil.disk_usage(d).free < info.file_size + HEADROOM:
                        raise Refused("There isn't room on this server to unpack that.")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        with z.open(info) as src:
                            total += _copy_capped(src, target, min(MAX_MEMBER, MAX_TOTAL - total))
                    except (zipfile.BadZipFile, RuntimeError, NotImplementedError, EOFError):
                        raise Refused("Part of that zip is damaged or locked with a password. Make the zip again.")
                    has["submissions"] |= name.endswith("data/submissions.db")
                    has["logins"] |= name.endswith("data/accounts.db")
            (d / f"{fid}.zip").unlink()                # the zip itself is not kept once read
        if not (dest / "docs" / "data").is_dir():
            raise Refused("That zip doesn't look like a PostyBirb folder. Zip the whole PostyBirb folder from Documents.")
    except Exception:
        _wipe(d)                                  # a refusal or a crash leaves nothing behind
        raise
    meta["ready"] = True
    (d / "session.json").write_text(json.dumps(meta), "utf-8")
    logger.info("PostyBirb upload: %d files taken out", count)
    return has


def base(uid: str) -> Path:
    d = _dir(uid)
    if not _meta(d).get("ready"):
        raise KeyError(uid)
    _touch(d)
    return d / "x"


def delete(uid: str) -> None:
    """Caller holds LOCK."""
    try:
        _wipe(_dir(uid))
    except KeyError:
        pass


def sweep(now: float | None = None, everything: bool = False) -> int:
    """Delete sessions idle for 30 minutes (all of them at start-up). Skips while an import runs."""
    r = root()
    if not r.is_dir() or not LOCK.acquire(blocking=False):
        return 0
    try:
        now = time.time() if now is None else now
        n = 0
        for d in r.iterdir():
            meta = d / "session.json"
            if everything or not meta.is_file() or now - meta.stat().st_mtime > IDLE_SECS:
                _wipe(d)
                n += 1
        if n:
            logger.info("PostyBirb upload: %d old session(s) deleted", n)
        return n
    finally:
        LOCK.release()
