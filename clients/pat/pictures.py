"""Downloading one picture from Patreon's picture host (spec 087, FR-SEC-002, 003, 011).

A picture address opens a members-only file, so this is deliberately narrower than the importer's shared client:

- https, port 443, no user part, and a host on spec 084's list (``MEDIA_HOST_SUFFIXES``), nothing else;
- a fresh client with no cookie and no Patreon header: the sign-in goes to www.patreon.com and nowhere else;
- no redirect is followed (a redirect is a failure), and the public-address check still runs;
- the body is read in pieces and stopped at the Library's picture limit; the file is accepted by its first bytes
  only (PNG, JPEG, GIF, WebP) and its extension comes from those bytes;
- every failure is a fixed sentence. The address never appears in an error, a log line or a result.
"""
from __future__ import annotations

import logging
from urllib.parse import urlsplit

import httpx

from clients.pat.client import MEDIA_HOST_SUFFIXES
from posting import pat_import, video_media

MAX_BYTES = 50 * 1024 * 1024        # the Library's own upload limit (routes/artwork_api._MAX_UPLOAD_BYTES)
TIMEOUT = 60.0
_MAGIC = ((b"\x89PNG\r\n\x1a\n", ".png"), (b"\xff\xd8\xff", ".jpg"), (b"GIF87a", ".gif"), (b"GIF89a", ".gif"))
# Stand-in transport for tests; None = the real network.
_transport = None


class PictureError(ValueError):
    """A picture that couldn't be brought in. ``blocked``: the picture host itself turned PawPoller away."""

    def __init__(self, sentence: str, blocked: bool = False):
        super().__init__(sentence)
        self.blocked = blocked


def host_ok(url: str) -> bool:
    try:
        p = urlsplit(str(url or ""))
        port = p.port
    except ValueError:
        return False
    host = (p.hostname or "").lower()
    return (p.scheme == "https" and port in (None, 443) and not p.username and not p.password
            and any(host.endswith(s) for s in MEDIA_HOST_SUFFIXES))


def _ext(head: bytes) -> str:
    for sig, ext in _MAGIC:
        if head[:len(sig)] == sig:
            return ext
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp"
    return ""


def fetch(url: str) -> tuple[bytes, str]:
    """(bytes, extension) for one picture, or PictureError with a fixed sentence."""
    if not host_ok(url):
        raise PictureError(pat_import.PICTURE_FAILED)
    quiet = logging.getLogger("httpx")      # httpx logs each request line, address included, at INFO
    was = quiet.level
    quiet.setLevel(logging.WARNING)
    try:
        with httpx.Client(follow_redirects=False, timeout=TIMEOUT, transport=_transport,
                          event_hooks={"request": [video_media._guard]}) as client:
            with client.stream("GET", url) as r:
                if r.status_code == 429:             # the host itself asking PawPoller to slow down
                    raise PictureError(pat_import.PICTURE_FAILED, blocked=True)
                # A 403 is an address whose time ran out as much as a refusal: one picture that didn't come.
                if r.status_code != 200:             # a redirect included: none is followed
                    raise PictureError(pat_import.PICTURE_FAILED)
                data = bytearray()
                for chunk in r.iter_bytes():
                    data += chunk
                    if len(data) > MAX_BYTES:
                        raise PictureError(pat_import.PICTURE_TOO_LARGE)
    except PictureError:
        raise
    except Exception:
        raise PictureError(pat_import.PICTURE_FAILED) from None     # never the library's own text: it has the address
    finally:
        quiet.setLevel(was)
    ext = _ext(bytes(data[:16]))
    if not ext:
        raise PictureError(pat_import.NOT_A_PICTURE)
    return bytes(data), ext
