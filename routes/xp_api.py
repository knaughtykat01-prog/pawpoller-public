"""Experience points and levels (XPLEVELS, 4.84.0, spec 050): ``GET /api/xp``.

Sweeps new posting rows into the ledger (the first call is the one-off
backfill), then returns the totals the level bar, the +XP chip and Laurels draw.
Shape: ``specs/050-xp-levels/contracts/xp-api.md``. Logic lives in ``xp.py``.

Level rewards (spec 051) add reveal and equip; shape in
``specs/051-level-rewards/contracts/rewards-api.md``, logic in ``rewards.py``.
"""
import hashlib
import io

from fastapi import APIRouter, HTTPException, Request, Response
from starlette.concurrency import run_in_threadpool
from PIL import Image, ImageOps

import config
import rewards
import xp
from database.db import get_connection
from routes.body_caps import check_declared

xp_router = APIRouter(prefix="/api/xp", tags=["xp"])

_AVATAR_MAX_BYTES = 2 * 1024 * 1024
_AVATAR_MAX_SIDE = 4096
_AVATAR_SIZE = 256
_FORM_OVERHEAD = 64 * 1024     # multipart boundaries + the small text fields
_WORK_NAME_MAX = 512


@xp_router.get("")
def get_xp():
    conn = get_connection()
    try:
        xp.sync(conn)
        return xp.totals(conn)
    finally:
        conn.close()


@xp_router.get("/history")
def get_history():
    conn = get_connection()
    try:
        return rewards.history(conn)
    finally:
        conn.close()


@xp_router.post("/rewards/reveal")
def reveal_rewards(body: dict):
    conn = get_connection()
    try:
        return {"revealed": rewards.reveal(conn, body.get("ids"))}
    finally:
        conn.close()


@xp_router.post("/rewards/equip")
def equip_reward(body: dict):
    kind, rid = body.get("kind"), body.get("id")
    if kind not in ("ring", "style"):
        raise HTTPException(400, "kind must be ring or style")
    conn = get_connection()
    try:
        if rid is not None:
            have = rewards.unlocked_ids(conn)
            item = next((c for c in rewards.catalog(have) if c["id"] == rid), None)
            if rid not in have or not item or item["kind"] != kind:
                raise HTTPException(409, "That reward isn't unlocked")
        eq = dict(config.get_settings().get("rewards_equipped") or {})
        eq[kind] = rid
        config.save_settings({"rewards_equipped": eq})
        return rewards.equipped(conn)
    finally:
        conn.close()


# ── Profile picture (US8): one row, re-encoded so nothing but pixels is kept ──

@xp_router.get("/avatar")
def get_avatar(request: Request):
    conn = get_connection()
    try:
        row = conn.execute("SELECT image FROM profile_picture WHERE id = 1").fetchone()
    finally:
        conn.close()
    if not row:
        raise HTTPException(404, "No picture")
    etag = '"' + hashlib.sha256(row[0]).hexdigest()[:32] + '"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag})
    return Response(row[0], media_type="image/webp", headers={"ETag": etag, "Cache-Control": "private, no-cache"})


def _reencode(data: bytes) -> bytes:
    """Centre-crop to a square, 256 px WebP. Re-encoding drops EXIF and anything else
    riding along in the file; a size check comes before the full decode."""
    try:
        with Image.open(io.BytesIO(data)) as im:
            if im.format not in ("PNG", "JPEG", "WEBP"):
                raise ValueError(im.format)
            if max(im.size) > _AVATAR_MAX_SIDE:
                raise HTTPException(400, f"Pictures can be up to {_AVATAR_MAX_SIDE} px on a side")
            im = ImageOps.exif_transpose(im).convert("RGBA")
            im = ImageOps.fit(im, (_AVATAR_SIZE, _AVATAR_SIZE), Image.LANCZOS)
            out = io.BytesIO()
            im.save(out, "WEBP", quality=85)
            return out.getvalue()
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(400, "That file isn't a picture PawPoller can read (PNG, JPEG or WebP)")


@xp_router.put("/avatar")
async def put_avatar(request: Request):
    # Takes Request, not File/Form params: those make FastAPI spool the whole body before this
    # runs, so the 2 MB cap came too late (SEC485, spec 054). The declared length bounds the body.
    too_big = "Pictures can be up to 2 MB"
    check_declared(request, _AVATAR_MAX_BYTES + _FORM_OVERHEAD, required=True, too_big=too_big)
    async with request.form(max_files=1, max_fields=8) as form:   # closes the spooled file
        image, source = form.get("image"), form.get("source") or "upload"
        work_name, rating = form.get("work_name"), form.get("rating") or "general"
        if image is None or isinstance(image, str):
            raise HTTPException(400, "image is required")
        if source not in ("work", "upload"):
            raise HTTPException(400, "source must be work or upload")
        if rating not in ("general", "mature", "adult"):
            raise HTTPException(400, "rating must be general, mature or adult")
        if work_name is not None and (not isinstance(work_name, str) or len(work_name) > _WORK_NAME_MAX):
            raise HTTPException(400, "work_name is too long")
        data = await image.read(_AVATAR_MAX_BYTES + 1)
    if len(data) > _AVATAR_MAX_BYTES:
        raise HTTPException(413, too_big)
    webp = await run_in_threadpool(_reencode, data)   # Pillow work off the event loop
    conn = get_connection()
    try:
        conn.execute("INSERT OR REPLACE INTO profile_picture (id, image, source, work_name, rating, updated_at)"
                     " VALUES (1, ?, ?, ?, ?, ?)",
                     (webp, source, (work_name or None) if source == "work" else None, rating, xp._now_utc()))
        conn.commit()
        return rewards.avatar(conn)
    finally:
        conn.close()


@xp_router.delete("/avatar")
def delete_avatar():
    conn = get_connection()
    try:
        conn.execute("DELETE FROM profile_picture WHERE id = 1")
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()
