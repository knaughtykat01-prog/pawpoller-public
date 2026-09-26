"""Cover images kept on disk so a board shows offline (research R8).

Downloads need header auth (``TrelloClient.download``); the images are served
back by ``GET /api/trello/covers/{attachment_id}`` with the same path confinement
as ``/api/artwork/image``. Only covers are kept — other attachments stay links.
"""
from __future__ import annotations

import logging
import re
import uuid
from pathlib import Path

import config
from clients.trello.client import TrelloError

logger = logging.getLogger(__name__)

IMAGE_TYPES = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif",
               "image/webp": ".webp"}
MEDIA_TYPES = {v: k for k, v in IMAGE_TYPES.items()}
MAX_UPLOAD = 10 * 1024 * 1024
_SAFE = re.compile(r"^[A-Za-z0-9_]{1,64}$")


def root() -> Path:
    p = Path(config.DATA_DIR) / "trello_covers"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _ext(mime: str, file_name: str) -> str:
    if mime in IMAGE_TYPES:
        return IMAGE_TYPES[mime]
    suffix = Path(file_name or "").suffix.lower()
    return suffix if suffix in MEDIA_TYPES else ""


def path_for(attachment_id: str) -> Path | None:
    """The cached file, confined to the covers folder, or None."""
    if not _SAFE.match(attachment_id or ""):
        return None
    base = root().resolve()
    for p in base.glob(f"{attachment_id}.*"):
        rp = p.resolve()
        try:
            rp.relative_to(base)
        except ValueError:
            return None
        if rp.suffix.lower() in MEDIA_TYPES:
            return rp
    return None


def fetch_missing(conn, client, limit: int = 20) -> int:
    """Download covers the mirror knows about but does not have yet."""
    rows = conn.execute(
        "SELECT v.* FROM trello_covers v JOIN trello_cards c ON c.id = v.card_id "
        "WHERE (v.local_path IS NULL OR v.local_path = '') AND c.removed_at IS NULL "
        "AND v.url != '' LIMIT ?", (limit,)).fetchall()
    got = 0
    for r in rows:
        ext = _ext(r["mime"], r["file_name"])
        if not ext or not _SAFE.match(r["attachment_id"] or ""):
            conn.execute("UPDATE trello_covers SET local_path = '-' WHERE attachment_id = ?",
                         (r["attachment_id"],))
            continue
        try:
            data = client.download(r["url"])
        except TrelloError as e:
            logger.info("Trello cover not fetched: %s", type(e).__name__)
            continue
        name = f"{r['attachment_id']}{ext}"
        (root() / name).write_bytes(data)
        conn.execute("UPDATE trello_covers SET local_path = ?, fetched_at = datetime('now') "
                     "WHERE attachment_id = ?", (name, r["attachment_id"]))
        got += 1
    conn.commit()
    return got


_MAGIC = {"image/png": (b"\x89PNG",), "image/jpeg": (b"\xff\xd8\xff",),
          "image/gif": (b"GIF87a", b"GIF89a"), "image/webp": (b"RIFF",)}


def stage(content: bytes, mime: str) -> str:
    """Keep an upload until the outbox sends it. Returns the staged name.

    The declared type is the browser's claim; the first bytes must agree."""
    if mime not in IMAGE_TYPES or not content.startswith(_MAGIC[mime]) or (
            mime == "image/webp" and content[8:12] != b"WEBP"):
        raise ValueError("Covers must be PNG, JPEG, GIF or WebP images.")
    if len(content) > MAX_UPLOAD:
        raise ValueError("That image is over 10 MB.")
    name = f"staged_{uuid.uuid4().hex}{IMAGE_TYPES[mime]}"
    (root() / name).write_bytes(content)
    return name


def staged_path(name: str) -> Path:
    if not re.match(r"^staged_[0-9a-f]{32}\.[a-z]{3,4}$", name or ""):
        raise ValueError("Unknown staged upload.")
    return root() / name


def adopt_staged(conn, staged: str, attachment: dict, card_id: str) -> None:
    """The upload was accepted: file it under Trello's attachment id."""
    att_id = attachment.get("id", "")
    src = staged_path(staged)
    if not att_id or not _SAFE.match(att_id):
        return
    dest = root() / f"{att_id}{src.suffix}"
    src.replace(dest)
    conn.execute(
        "INSERT INTO trello_covers (attachment_id, card_id, url, file_name, mime, local_path, "
        "fetched_at) VALUES (?, ?, ?, ?, ?, ?, datetime('now')) "
        "ON CONFLICT(attachment_id) DO UPDATE SET local_path = excluded.local_path",
        (att_id, card_id, attachment.get("url", ""), attachment.get("fileName", ""),
         MEDIA_TYPES.get(src.suffix, ""), dest.name))
