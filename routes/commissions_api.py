"""Commissions API — a lightweight client/commission tracker (gap-wave-5 §4).

Single self-contained resource (no polymorphic members like Collections). Money
is data only — no payment integration. `artwork_name` deep-links a delivered
piece; `deliver_sites` is a JSON array of platform codes from the poster set.
"""
import logging
import os
import re
import shutil
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse

import config
from database.db import get_connection
from database import commissions_queries as cq

logger = logging.getLogger(__name__)

commissions_router = APIRouter(prefix="/api/commissions", tags=["commissions"])

# ── Attachments (2.188) ──────────────────────────────────────────────
# Files live on disk under the persistent data volume, NOT in SQLite. One
# folder per commission; the directory listing IS the file list.
_MAX_FILE_BYTES = 25 * 1024 * 1024   # 25 MB/file
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}
_IMAGE_MEDIA = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp"}


def _files_dir(cid: int) -> Path:
    """Per-commission attachments folder (created lazily). Reads config.DATA_DIR
    at call time so tests can redirect it."""
    return Path(config.DATA_DIR) / "commission_files" / str(int(cid))


def _remove_files_dir(cid: int) -> None:
    d = _files_dir(cid)
    if d.is_dir():
        shutil.rmtree(d, ignore_errors=True)


def _safe_name(name: str) -> str:
    """Basename only, keep [A-Za-z0-9._ -], other chars → '_', cap length,
    non-empty fallback. Blocks path traversal at the source (no separators survive)."""
    base = os.path.basename(name or "").strip()
    base = re.sub(r"[^A-Za-z0-9._ -]", "_", base).strip(". ")
    if len(base) > 120:
        root, ext = os.path.splitext(base)
        base = root[:120 - len(ext)] + ext
    return base or "file"


def _dedupe_name(folder: Path, name: str) -> str:
    """Avoid clobbering an existing file: 'ref.png' → 'ref (2).png'."""
    if not (folder / name).exists():
        return name
    root, ext = os.path.splitext(name)
    n = 2
    while (folder / f"{root} ({n}){ext}").exists():
        n += 1
    return f"{root} ({n}){ext}"


def _resolve_attachment(cid: int, filename: str) -> Path:
    """Resolve {filename} inside the commission folder with a traversal guard
    (mirrors posting_api.get_story_image). Raises 400/404 on escape/miss."""
    folder = _files_dir(cid).resolve()
    requested = (folder / filename).resolve()
    try:
        requested.relative_to(folder)
    except ValueError:
        raise HTTPException(400, detail="Invalid filename")
    if not requested.is_file():
        raise HTTPException(404, detail="File not found")
    return requested


@commissions_router.get("")
def list_commissions(archived: int = 0):
    """Commissions, soonest-due first. Active by default; `?archived=1` for the
    archived pile. Includes the status vocabulary (so the board columns / advance
    control don't hardcode it) and the archived count (for the toggle label)."""
    conn = get_connection()
    try:
        return {
            "commissions": cq.list_commissions(conn, archived=bool(archived)),
            "statuses": list(cq.STATUSES),
            "archived_count": cq.count_archived(conn),
        }
    finally:
        conn.close()


@commissions_router.post("")
def create_commission(body: dict):
    """Create a commission. Body: {client_name, description?, price?, currency?,
    status?, due_date?, artwork_name?, deliver_sites?, notes?}."""
    client = (body.get("client_name") or "").strip()
    if not client:
        raise HTTPException(400, detail="client_name is required")
    status = body.get("status", "quote")
    if status and status not in cq.STATUSES:
        raise HTTPException(400, detail=f"status must be one of: {', '.join(cq.STATUSES)}")
    conn = get_connection()
    try:
        cid = cq.create_commission(
            conn, client_name=client, description=body.get("description", ""),
            price=body.get("price", 0), currency=body.get("currency", "USD"),
            status=status or "quote", due_date=body.get("due_date", ""),
            artwork_name=body.get("artwork_name", ""),
            deliver_sites=body.get("deliver_sites", []), notes=body.get("notes", ""))
        conn.commit()
        return {"status": "created", "id": cid}
    finally:
        conn.close()


@commissions_router.get("/{cid}")
def get_commission(cid: int):
    conn = get_connection()
    try:
        row = cq.get_commission(conn, cid)
        if not row:
            raise HTTPException(404, detail="Commission not found")
        return row
    finally:
        conn.close()


@commissions_router.patch("/{cid}")
def update_commission(cid: int, body: dict):
    """Update any of the commission fields (status validated against the set)."""
    status = body.get("status")
    if status is not None and status not in cq.STATUSES:
        raise HTTPException(400, detail=f"status must be one of: {', '.join(cq.STATUSES)}")
    conn = get_connection()
    try:
        if not cq.get_commission(conn, cid):
            raise HTTPException(404, detail="Commission not found")
        cq.update_commission(
            conn, cid, client_name=body.get("client_name"),
            description=body.get("description"), price=body.get("price"),
            currency=body.get("currency"), status=status,
            due_date=body.get("due_date"), artwork_name=body.get("artwork_name"),
            deliver_sites=body.get("deliver_sites"), notes=body.get("notes"))
        conn.commit()
        return {"status": "updated"}
    finally:
        conn.close()


@commissions_router.delete("/{cid}")
def delete_commission(cid: int):
    conn = get_connection()
    try:
        cq.delete_commission(conn, cid)
        conn.commit()
    finally:
        conn.close()
    # Best-effort: drop the attachments folder too (files aren't in the DB).
    _remove_files_dir(cid)
    return {"status": "deleted"}


# ── Attachment endpoints (2.188) ─────────────────────────────────────

@commissions_router.get("/{cid}/files")
def list_files(cid: int):
    """List a commission's attachments (newest first). No DB row — the folder
    listing is the source of truth."""
    folder = _files_dir(cid)
    files = []
    if folder.is_dir():
        for p in folder.iterdir():
            if not p.is_file():
                continue
            st = p.stat()
            ext = p.suffix.lower()
            files.append({
                "filename": p.name,
                "size": st.st_size,
                "uploaded_at": st.st_mtime,
                "is_image": ext in _IMAGE_EXTS,
                "url": f"/api/commissions/{cid}/files/{p.name}",
            })
    files.sort(key=lambda f: f["uploaded_at"], reverse=True)
    return {"files": files}


@commissions_router.post("/{cid}/files")
async def upload_file(cid: int, file: UploadFile = File(...)):
    """Attach any file to a commission (≤25 MB). Reference sheets, WIPs,
    screenshots, contracts, source zips — whatever."""
    conn = get_connection()
    try:
        if not cq.get_commission(conn, cid):
            raise HTTPException(404, detail="Commission not found")
    finally:
        conn.close()

    data = await file.read()
    if len(data) > _MAX_FILE_BYTES:
        raise HTTPException(413, detail=f"File too large (max {_MAX_FILE_BYTES // (1024*1024)} MB)")
    if not data:
        raise HTTPException(400, detail="Empty file")

    folder = _files_dir(cid)
    folder.mkdir(parents=True, exist_ok=True)
    name = _dedupe_name(folder, _safe_name(file.filename))
    (folder / name).write_bytes(data)
    return {"status": "uploaded", "filename": name, "size": len(data)}


@commissions_router.get("/{cid}/files/{filename}")
def download_file(cid: int, filename: str):
    """Serve one attachment. Images render inline; everything else downloads as
    an octet-stream attachment (a stored .html can't execute in the session)."""
    path = _resolve_attachment(cid, filename)
    ext = path.suffix.lower()
    if ext in _IMAGE_MEDIA:
        return FileResponse(str(path), media_type=_IMAGE_MEDIA[ext],
                            headers={"Cache-Control": "private, max-age=3600"})
    return FileResponse(
        str(path), media_type="application/octet-stream", filename=path.name,
        headers={"Content-Disposition": f'attachment; filename="{path.name}"',
                 "X-Content-Type-Options": "nosniff"})


@commissions_router.delete("/{cid}/files/{filename}")
def delete_file(cid: int, filename: str):
    """Remove one attachment."""
    path = _resolve_attachment(cid, filename)
    try:
        path.unlink()
    except OSError as e:
        raise HTTPException(500, detail=f"Could not delete: {e}")
    return {"status": "deleted"}
