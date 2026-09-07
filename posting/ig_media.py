"""Temporary public hosting for Instagram post images — 2.64.0.

Instagram's Content Publishing API does NOT accept uploaded image bytes for
photos: you pass ``image_url`` and Meta's servers cURL it. So to post an image
PawPoller stashes a web-safe JPEG copy on the data volume and serves it,
unauthenticated, at ``/api/ig/pubmedia/<token>`` for a short window, then deletes
it once Meta has fetched + published. Tokens are unguessable (uuid4 hex) and any
stragglers self-expire on the next stash, so the public exposure is limited to
the few seconds of an active publish (and the image is about to be public on
Instagram anyway).

Server-only by nature — the image URL must be reachable by Meta, which only works
from the deployment that sits behind a public address (``ig_public_base_url``).
Since 4.7.0 the same stash also backs the open relay (``/api/ig/relay``) and the
desktop's temporary tunnel server (``posting/ig_tunnel.py``) — see ``posting/ig_host.py``.
"""
from __future__ import annotations
import io
import re
import time
import uuid
from pathlib import Path

import config

_TTL_SECONDS = 900          # 15 min — stale stashes are swept on the next stash
TTL_SECONDS = _TTL_SECONDS
_MAX_EDGE = 1440            # IG's max recommended width; downscale the long edge
_TOKEN_RE = re.compile(r"^[a-f0-9]{32}$")


def _dir() -> Path:
    d = config.DATA_DIR / "ig_pending"
    d.mkdir(parents=True, exist_ok=True)
    return d


# 4.20.1 (MEDIATYPES phase 3): the stash also hosts a video for an Instagram Reel
# (Meta cURLs `video_url` the way it cURLs `image_url`). A video is stored as its
# own bytes under its own extension — never re-encoded, PawPoller does not decode
# media — so the serving routes label it by extension.
VIDEO_EXTS = (".mp4", ".mov")
_MIME = {".jpg": "image/jpeg", ".mp4": "video/mp4", ".mov": "video/quicktime"}
_VIDEO_MAGIC_OFFSET_4 = b"ftyp"          # ISO-BMFF (mp4 / mov): bytes 4..8 read "ftyp"


def is_video_bytes(data: bytes) -> bool:
    """True for an mp4 / mov container (the `ftyp` box at offset 4)."""
    return len(data) > 12 and data[4:8] == _VIDEO_MAGIC_OFFSET_4


def mime_for(path) -> str:
    return _MIME.get(Path(str(path)).suffix.lower(), "application/octet-stream")


def sweep() -> None:
    """Delete any stashed files older than the TTL (best-effort)."""
    now = time.time()
    for f in _dir().glob("*.*"):
        try:
            if now - f.stat().st_mtime > _TTL_SECONDS:
                f.unlink()
        except OSError:
            pass


def _stash(img) -> str:
    """Normalise a PIL image to a web-safe JPEG, stash it, return its hex token.

    Instagram only accepts JPEG, so PNG/WebP/etc. are converted; oversized images
    are downscaled to ``_MAX_EDGE`` on the long edge to stay under IG's 8 MB limit.
    """
    if img.mode != "RGB":
        img = img.convert("RGB")
    w, h = img.size
    if max(w, h) > _MAX_EDGE:
        scale = _MAX_EDGE / float(max(w, h))
        img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=88)
    token = uuid.uuid4().hex
    (_dir() / f"{token}.jpg").write_bytes(buf.getvalue())
    return token


def stash_image(source_path: str) -> str:
    """Convert a file at *source_path* to a stashed web-safe JPEG; return its token."""
    from PIL import Image
    sweep()
    return _stash(Image.open(source_path))


def stash_bytes(data: bytes) -> str:
    """Stash raw *data* and return its token: an image is normalised to a web-safe
    JPEG; an mp4 / mov (4.20.1) is kept byte-for-byte under ``.mp4`` / ``.mov``.

    Used by the authenticated ``POST /api/ig/pubmedia`` relay endpoint, so a
    paired desktop instance (no public address of its own) can hand a file to
    this public server to host during an Instagram publish.
    """
    sweep()
    if is_video_bytes(data):
        return _stash_raw(data, ".mov" if data[8:12] == b"qt  " else ".mp4")
    from PIL import Image
    return _stash(Image.open(io.BytesIO(data)))


def stash_file(source_path: str) -> str:
    """Stash a video file as it is (4.20.1); return its token. Images go through
    :func:`stash_image` (normalised); this never re-encodes."""
    sweep()
    ext = Path(source_path).suffix.lower()
    if ext not in VIDEO_EXTS:
        raise ValueError(f"stash_file takes {', '.join(VIDEO_EXTS)} — not {ext or 'this file'}")
    return _stash_raw(Path(source_path).read_bytes(), ext)


def _stash_raw(data: bytes, ext: str) -> str:
    token = uuid.uuid4().hex
    (_dir() / f"{token}{ext}").write_bytes(data)
    return token


def pending_count() -> int:
    """How many files are hosted right now (after a sweep) — the relay's cap."""
    sweep()
    return sum(1 for _ in _dir().glob("*.*"))


def _find(token: str) -> Path | None:
    for ext in (".jpg",) + VIDEO_EXTS:
        p = _dir() / f"{token}{ext}"
        if p.exists():
            return p
    return None


def path_for(token: str) -> Path | None:
    """Resolve a request token to its stashed file, or None if invalid/missing.

    Guards against path traversal: only a bare 32-char hex token (optionally with
    a ``.jpg`` / ``.mp4`` / ``.mov`` suffix from the URL) maps to a file inside
    the pending dir; the suffix is decorative — the token decides.
    """
    for ext in (".jpg",) + VIDEO_EXTS:
        if token.endswith(ext):
            token = token[:-len(ext)]
            break
    if not _TOKEN_RE.match(token):
        return None
    return _find(token)


def ext_for(token: str) -> str:
    """The stashed file's extension for *token* (``.jpg`` when unknown)."""
    p = _find(token) if _TOKEN_RE.match(token or "") else None
    return p.suffix if p else ".jpg"


def public_url(base_url: str, token: str) -> str:
    """Build the public URL Meta will fetch (the file's own suffix for friendliness)."""
    return f"{base_url.rstrip('/')}/api/ig/pubmedia/{token}{ext_for(token)}"


def cleanup(token: str) -> None:
    """Delete a stashed file once its publish is done (best-effort)."""
    p = _find(token) if _TOKEN_RE.match(token or "") else None
    if p:
        try:
            p.unlink()
        except OSError:
            pass
