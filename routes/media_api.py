"""Media inbox for connected desktops (SYNCTRUTH, 4.13.0) — ``/api/media/*``.

A connected desktop cannot hand the server a *path*; it hands it the *file*. Uploads land
in ``DATA_DIR/inbox`` under a content-addressed name, and the response carries the server-side
path the existing "create artwork from path" route accepts. ``exists`` lets the agent skip a
re-upload after a retry. Auth is the ordinary API-key bearer check in dashboard.py.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

import config

logger = logging.getLogger(__name__)
media_router = APIRouter(prefix="/api/media", tags=["media"])

INBOX = config.DATA_DIR / "inbox"
MEDIA_UPLOAD_MAX_BYTES = 200 * 1024 * 1024
_SAFE = re.compile(r"[^A-Za-z0-9._ ()-]+")      # a basename only; anything odd becomes '_'
_lock = threading.Lock()


def _index_path() -> Path:
    return INBOX / "index.json"


def _load_index() -> dict:
    try:
        with open(_index_path(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_index(index: dict) -> None:
    INBOX.mkdir(parents=True, exist_ok=True)
    tmp = _index_path().with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(index, f, indent=1)
    tmp.replace(_index_path())


def safe_name(filename: str) -> str:
    base = Path(filename or "").name
    base = _SAFE.sub("_", base).strip(" .") or "upload"
    return base[:120]


def lookup(sha256: str) -> Path | None:
    rel = _load_index().get(sha256)
    if not rel:
        return None
    p = INBOX / rel
    return p if p.is_file() else None


@media_router.get("/exists/{sha256}")
def media_exists(sha256: str):
    sha256 = sha256.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise HTTPException(400, "sha256 must be 64 hex characters")
    p = lookup(sha256)
    return {"exists": p is not None, "path": str(p) if p else None}


@media_router.post("/upload")
async def media_upload(file: UploadFile = File(...), kind: str = Form("inbox"), sha256: str = Form("")):
    kind = (kind or "inbox").strip().lower()
    if kind not in ("inbox", "artwork", "story", "post"):
        raise HTTPException(400, "kind must be inbox, artwork, story or post")
    h = hashlib.sha256()
    size = 0
    INBOX.mkdir(parents=True, exist_ok=True)
    tmp = INBOX / f".upload-{threading.get_ident()}-{id(file)}.part"
    try:
        with open(tmp, "wb") as out:
            while True:
                chunk = await file.read(1 << 20)
                if not chunk:
                    break
                size += len(chunk)
                if size > MEDIA_UPLOAD_MAX_BYTES:
                    raise HTTPException(413, f"file exceeds {MEDIA_UPLOAD_MAX_BYTES // (1024 * 1024)} MB")
                h.update(chunk)
                out.write(chunk)
        digest = h.hexdigest()
        if sha256 and sha256.strip().lower() != digest:
            raise HTTPException(400, "sha256 does not match the uploaded bytes")
        if size == 0:
            raise HTTPException(400, "empty file")
        name = f"{digest[:16]}_{safe_name(file.filename)}"
        dest = INBOX / name
        with _lock:
            existing = lookup(digest)
            if existing is not None:
                tmp.unlink(missing_ok=True)
                return {"path": str(existing), "sha256": digest, "size": existing.stat().st_size, "existing": True}
            tmp.replace(dest)
            index = _load_index()
            index[digest] = name
            _save_index(index)
        logger.info("Media inbox: received %s (%d bytes, kind=%s)", name, size, kind)
        return {"path": str(dest), "sha256": digest, "size": size, "existing": False}
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
