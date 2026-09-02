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
"""
from __future__ import annotations
import io
import re
import time
import uuid
from pathlib import Path

import config

_TTL_SECONDS = 900          # 15 min — stale stashes are swept on the next stash
_MAX_EDGE = 1440            # IG's max recommended width; downscale the long edge
_TOKEN_RE = re.compile(r"^[a-f0-9]{32}$")


def _dir() -> Path:
    d = config.DATA_DIR / "ig_pending"
    d.mkdir(parents=True, exist_ok=True)
    return d


def sweep() -> None:
    """Delete any stashed images older than the TTL (best-effort)."""
    now = time.time()
    for f in _dir().glob("*.jpg"):
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
    """Convert raw image *data* to a stashed web-safe JPEG; return its token.

    Used by the authenticated ``POST /api/ig/pubmedia`` relay endpoint, so a
    paired desktop instance (no public address of its own) can hand an image to
    this public server to host during an Instagram publish.
    """
    from PIL import Image
    sweep()
    return _stash(Image.open(io.BytesIO(data)))


def path_for(token: str) -> Path | None:
    """Resolve a request token to its stashed file, or None if invalid/missing.

    Guards against path traversal: only a bare 32-char hex token (optionally with
    a ``.jpg`` suffix from the URL) maps to a file inside the pending dir.
    """
    if token.endswith(".jpg"):
        token = token[:-4]
    if not _TOKEN_RE.match(token):
        return None
    p = _dir() / f"{token}.jpg"
    return p if p.exists() else None


def public_url(base_url: str, token: str) -> str:
    """Build the public URL Meta will fetch (``.jpg`` suffix for friendliness)."""
    return f"{base_url.rstrip('/')}/api/ig/pubmedia/{token}.jpg"


def cleanup(token: str) -> None:
    """Delete a stashed image once its publish is done (best-effort)."""
    try:
        (_dir() / f"{token}.jpg").unlink()
    except OSError:
        pass
