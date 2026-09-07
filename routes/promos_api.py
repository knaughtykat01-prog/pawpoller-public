"""Saved promo cards — ``/api/promos`` (Promo Maker v2 release 2, 4.16.0).

Spec: docs/specs/promo_maker_v2.md §3. A card is stored as its editable spec (JSON) plus
the rendered PNG (and the photo background when one was used). Files live under
``DATA_DIR/promos`` and are addressed only through the row's stored file names, so no
request can name a path. A promo is a generated image, not artwork: deleting one is a
hard delete of the row and its files (the UI confirms once).

Routes (literal paths before ``/{promo_id}``):

* ``GET    /api/promos?story=<name>``   list, newest first
* ``GET    /api/promos/{id}``           the row with the parsed ``spec``
* ``GET    /api/promos/{id}/image``     the PNG (no-store: it changes on save)
* ``GET    /api/promos/{id}/background``the photo, 404 when the card has none
* ``POST   /api/promos``                multipart ``spec`` + ``png`` [+ ``background``] [+ ``title``, ``story_name``] → 201
* ``PUT    /api/promos/{id}``           same parts; a new ``background`` replaces, ``clear_background=1`` removes
* ``DELETE /api/promos/{id}``
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, Response

import config
from database import promos as pdb
from database.db import get_connection

logger = logging.getLogger(__name__)
promos_router = APIRouter(prefix="/api/promos")

MAX_PNG_BYTES = 12 * 1024 * 1024
MAX_BG_BYTES = 12 * 1024 * 1024
MAX_TEXT = 20_000
MIN_SIDE, MAX_SIDE = 320, 4096
_BG_EXT = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif"}


def promos_dir() -> Path:
    d = config.DATA_DIR / "promos"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _file(name: str) -> Path | None:
    """A stored file name → its path, or None when it is missing or escapes the dir."""
    if not name:
        return None
    base = promos_dir().resolve()
    p = (base / name).resolve()
    if p.parent != base or not p.is_file():
        return None
    return p


def _story_exists(name: str) -> bool:
    """A story the editor would list: a folder under the archive holding a MASTER.md or story.json."""
    from posting.story_reader import get_archive_path
    try:
        archive = get_archive_path().resolve()
        p = (archive / name).resolve()
    except Exception:
        return False
    if archive not in p.parents:
        return False
    return (p / "Markdown" / "MASTER.md").is_file() or (p / "story.json").is_file()


def validate_spec(raw: str) -> tuple[dict, int, int]:
    """Parse + sanity-check a card spec. Returns (spec, width, height) or raises ValueError."""
    try:
        spec = json.loads(raw or "")
    except Exception:
        raise ValueError("spec must be JSON")
    if not isinstance(spec, dict) or spec.get("version") != 2:
        raise ValueError("spec must be a version-2 promo spec")
    text = spec.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("spec.text is required")
    if len(text) > MAX_TEXT:
        raise ValueError(f"spec.text is longer than {MAX_TEXT} characters")
    size = spec.get("size") or {}
    try:
        w, h = int(size.get("w")), int(size.get("h"))
    except Exception:
        raise ValueError("spec.size.w / spec.size.h are required integers")
    if not (MIN_SIDE <= w <= MAX_SIDE and MIN_SIDE <= h <= MAX_SIDE):
        raise ValueError(f"size must be within {MIN_SIDE}–{MAX_SIDE} px")
    n = len(text)
    for key in ("highlights", "styles"):
        ranges = spec.get(key) or []
        if not isinstance(ranges, list):
            raise ValueError(f"spec.{key} must be a list")
        for r in ranges:
            try:
                a, z = int(r["start"]), int(r["end"])
            except Exception:
                raise ValueError(f"spec.{key} entries need start/end")
            if not (0 <= a < z <= n):
                raise ValueError(f"spec.{key} range {a}-{z} is outside the text")
    return spec, w, h


def _public(row: dict) -> dict:
    out = {k: row[k] for k in ("promo_id", "story_name", "title", "width", "height", "created_at", "updated_at")}
    out["image_url"] = f"/api/promos/{row['promo_id']}/image"
    out["has_background"] = bool(row.get("bg_file"))
    try:
        pages = json.loads(row.get("spec_json") or "{}").get("pages")
        out["pages"] = len(pages) if isinstance(pages, list) and pages else 1
    except Exception:
        out["pages"] = 1
    return out


# ── the announcement image (4.17.0) ──────────────────────────────────────────
# story.json `images.promo` names the promo whose PNG Telegram / X / Bluesky carry for this
# story (posting/story_reader._promo_image). Written the way the editor writes story.json:
# a timestamped backup, then an atomic replace.

def _story_json_path(story_name: str) -> Path | None:
    from posting.story_reader import get_archive_path
    try:
        archive = get_archive_path().resolve()
        p = (archive / story_name).resolve()
    except Exception:
        return None
    if archive not in p.parents:
        return None
    sj = p / "story.json"
    return sj if sj.is_file() else None


def _announced_promo(story_name: str | None) -> int | None:
    sj = _story_json_path(story_name) if story_name else None
    if not sj:
        return None
    try:
        images = json.loads(sj.read_text(encoding="utf-8")).get("images") or {}
        return int(images.get("promo"))
    except Exception:
        return None


def _set_announced_promo(story_name: str, promo_id: int | None) -> None:
    from routes.editor_api import _backup_story_json
    sj = _story_json_path(story_name)
    if not sj:
        raise HTTPException(400, detail="This story has no story.json to record the choice in")
    try:
        data = json.loads(sj.read_text(encoding="utf-8"))
    except Exception:
        raise HTTPException(400, detail="story.json could not be read")
    images = data.get("images")
    if not isinstance(images, dict):
        images = {}
    if promo_id is None:
        images.pop("promo", None)
    else:
        images["promo"] = int(promo_id)
    data["images"] = images
    _backup_story_json(sj)
    tmp = sj.with_name("story.json.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(str(tmp), str(sj))


def _conn():
    conn = get_connection()
    pdb.ensure(conn)
    return conn


async def _read(upload: UploadFile | None, limit: int, what: str) -> bytes | None:
    if upload is None:
        return None
    data = await upload.read()
    if len(data) > limit:
        raise HTTPException(413, detail=f"{what} is larger than {limit // (1024 * 1024)} MB")
    return data


def _bg_ext(upload: UploadFile | None, data: bytes | None) -> str | None:
    if upload is None or not data:
        return None
    ext = _BG_EXT.get((upload.content_type or "").lower())
    if not ext:
        ext = Path(upload.filename or "").suffix.lower()
        if ext == ".jpeg":
            ext = ".jpg"
        if ext not in _BG_EXT.values():
            raise HTTPException(415, detail="background must be a PNG, JPEG, WebP or GIF image")
    return ext


def _write(path: Path, data: bytes) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _remove(name: str | None) -> None:
    p = _file(name or "")
    if p:
        try:
            p.unlink()
        except OSError as e:
            logger.warning("promo file %s not removed: %s", name, e)


def _check_story(story_name: str | None) -> str | None:
    story_name = (story_name or "").strip() or None
    if story_name and not _story_exists(story_name):
        raise HTTPException(400, detail="story_name is not a story the editor lists")
    return story_name


# ── routes ───────────────────────────────────────────────────────────────────

@promos_router.get("")
def list_promos(story: str | None = Query(None)):
    conn = _conn()
    try:
        rows = pdb.list_promos(conn, story_name=story or None)
    finally:
        conn.close()
    out = []
    chosen: dict[str, int | None] = {}
    for r in rows:
        pub = _public(r)
        name = r.get("story_name")
        if name and name not in chosen:
            chosen[name] = _announced_promo(name)
        pub["announce"] = bool(name) and chosen.get(name) == r["promo_id"]
        out.append(pub)
    return {"promos": out}


@promos_router.post("/{promo_id}/announce")
def announce_with_promo(promo_id: int):
    """Make this card the story's announcement image (story.json images.promo)."""
    conn = _conn()
    try:
        row = pdb.get_promo(conn, promo_id)
    finally:
        conn.close()
    if not row:
        raise HTTPException(404, detail="No such promo")
    if not row.get("story_name"):
        raise HTTPException(400, detail="This promo is not attached to a story")
    _set_announced_promo(row["story_name"], promo_id)
    return {"ok": True, "story_name": row["story_name"], "promo_id": promo_id, "announce": True}


@promos_router.delete("/{promo_id}/announce")
def stop_announcing_with_promo(promo_id: int):
    conn = _conn()
    try:
        row = pdb.get_promo(conn, promo_id)
    finally:
        conn.close()
    if not row:
        raise HTTPException(404, detail="No such promo")
    if row.get("story_name") and _announced_promo(row["story_name"]) == promo_id:
        _set_announced_promo(row["story_name"], None)
    return {"ok": True, "story_name": row.get("story_name"), "promo_id": promo_id, "announce": False}


@promos_router.post("", status_code=201)
async def create_promo(spec: str = Form(...), png: UploadFile = File(...),
                       background: UploadFile | None = File(None),
                       title: str = Form(""), story_name: str | None = Form(None)):
    try:
        parsed, w, h = validate_spec(spec)
    except ValueError as e:
        raise HTTPException(400, detail=str(e))
    story_name = _check_story(story_name)
    png_bytes = await _read(png, MAX_PNG_BYTES, "png")
    if not png_bytes or not png_bytes.startswith(b"\x89PNG"):
        raise HTTPException(415, detail="png must be a PNG image")
    bg_bytes = await _read(background, MAX_BG_BYTES, "background")
    bg_ext = _bg_ext(background, bg_bytes)

    conn = _conn()
    try:
        pid = pdb.create_promo(conn, story_name=story_name, title=title[:200], spec_json=json.dumps(parsed),
                               width=w, height=h)
        png_file = f"{pid}.png"
        bg_file = f"{pid}-bg{bg_ext}" if bg_ext else None
        d = promos_dir()
        _write(d / png_file, png_bytes)
        if bg_file:
            _write(d / bg_file, bg_bytes)
        pdb.set_files(conn, pid, png_file, bg_file)
        row = pdb.get_promo(conn, pid)
    finally:
        conn.close()
    return _public(row)


@promos_router.get("/{promo_id}")
def get_promo(promo_id: int):
    conn = _conn()
    try:
        row = pdb.get_promo(conn, promo_id)
    finally:
        conn.close()
    if not row:
        raise HTTPException(404, detail="No such promo")
    out = _public(row)
    try:
        out["spec"] = json.loads(row["spec_json"])
    except Exception:
        out["spec"] = None
    return out


@promos_router.get("/{promo_id}/image")
def get_promo_image(promo_id: int):
    conn = _conn()
    try:
        row = pdb.get_promo(conn, promo_id)
    finally:
        conn.close()
    p = _file(row["png_file"]) if row else None
    if not p:
        raise HTTPException(404, detail="Image not found")
    return FileResponse(str(p), media_type="image/png", headers={"Cache-Control": "no-store"})


@promos_router.get("/{promo_id}/background")
def get_promo_background(promo_id: int):
    conn = _conn()
    try:
        row = pdb.get_promo(conn, promo_id)
    finally:
        conn.close()
    p = _file(row["bg_file"] or "") if row else None
    if not p:
        raise HTTPException(404, detail="This promo has no photo background")
    return FileResponse(str(p), headers={"Cache-Control": "no-store"})


@promos_router.put("/{promo_id}")
async def update_promo(promo_id: int, spec: str = Form(...), png: UploadFile = File(...),
                       background: UploadFile | None = File(None), clear_background: str = Form("0"),
                       title: str = Form(""), story_name: str | None = Form(None)):
    try:
        parsed, w, h = validate_spec(spec)
    except ValueError as e:
        raise HTTPException(400, detail=str(e))
    story_name = _check_story(story_name)
    png_bytes = await _read(png, MAX_PNG_BYTES, "png")
    if not png_bytes or not png_bytes.startswith(b"\x89PNG"):
        raise HTTPException(415, detail="png must be a PNG image")
    bg_bytes = await _read(background, MAX_BG_BYTES, "background")
    bg_ext = _bg_ext(background, bg_bytes)

    conn = _conn()
    try:
        row = pdb.get_promo(conn, promo_id)
        if not row:
            raise HTTPException(404, detail="No such promo")
        d = promos_dir()
        bg_file = row.get("bg_file")
        if bg_ext:
            _remove(bg_file)
            bg_file = f"{promo_id}-bg{bg_ext}"
            _write(d / bg_file, bg_bytes)
        elif clear_background in ("1", "true", "yes"):
            _remove(bg_file)
            bg_file = None
        _write(d / row["png_file"], png_bytes)
        pdb.update_promo(conn, promo_id, story_name=story_name, title=title[:200], spec_json=json.dumps(parsed),
                         width=w, height=h, bg_file=bg_file)
        row = pdb.get_promo(conn, promo_id)
    finally:
        conn.close()
    return _public(row)


@promos_router.delete("/{promo_id}")
def delete_promo(promo_id: int):
    conn = _conn()
    try:
        row = pdb.get_promo(conn, promo_id)
        if not row:
            raise HTTPException(404, detail="No such promo")
        _remove(row.get("png_file"))
        _remove(row.get("bg_file"))
        pdb.delete_promo(conn, promo_id)
    finally:
        conn.close()
    if row.get("story_name") and _announced_promo(row["story_name"]) == promo_id:
        try:
            _set_announced_promo(row["story_name"], None)          # the story falls back to its cover
        except HTTPException:
            pass
    return Response(status_code=204)
