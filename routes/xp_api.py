"""Experience points and levels (XPLEVELS, 4.84.0, spec 050): ``GET /api/xp``.

Sweeps new posting rows into the ledger (the first call is the one-off
backfill), then returns the totals the level bar, the +XP chip and Laurels draw.
Shape: ``specs/050-xp-levels/contracts/xp-api.md``. Logic lives in ``xp.py``.

Level rewards (spec 051) add reveal and equip; shape in
``specs/051-level-rewards/contracts/rewards-api.md``, logic in ``rewards.py``.
"""
import hashlib
import io

from fastapi import APIRouter, File, Form, HTTPException, Request, Response, UploadFile
from PIL import Image, ImageOps

import config
import rewards
import xp
from database.db import get_connection

xp_router = APIRouter(prefix="/api/xp", tags=["xp"])

_AVATAR_MAX_BYTES = 2 * 1024 * 1024
_AVATAR_MAX_SIDE = 4096
_AVATAR_SIZE = 256


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
async def put_avatar(image: UploadFile = File(...), source: str = Form("upload"),
                     work_name: str | None = Form(None), rating: str = Form("general")):
    if source not in ("work", "upload"):
        raise HTTPException(400, "source must be work or upload")
    if rating not in ("general", "mature", "adult"):
        raise HTTPException(400, "rating must be general, mature or adult")
    data = await image.read(_AVATAR_MAX_BYTES + 1)
    if len(data) > _AVATAR_MAX_BYTES:
        raise HTTPException(413, "Pictures can be up to 2 MB")
    webp = _reencode(data)
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
