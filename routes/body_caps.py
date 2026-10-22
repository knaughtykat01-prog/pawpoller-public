"""Body size limits checked before a route reads the body (spec 054).

A route that declares ``UploadFile``/``Form`` gets its whole body parsed and
spooled before its own code runs, so a cap inside the handler comes too late.
Routes that face strangers take ``Request`` instead and call these first. The
server never reads past the declared Content-Length, so checking it bounds the
body; ``read_capped`` also covers a body sent with no length (chunked).
"""
from __future__ import annotations

from fastapi import HTTPException, Request


def check_declared(request: Request, cap: int, *, required: bool = False,
                   too_big: str = "Upload too large") -> int | None:
    """The declared Content-Length, refused when absent (if *required*),
    unreadable or over *cap*."""
    raw = request.headers.get("content-length")
    if raw is not None and "transfer-encoding" in request.headers:
        # Both is malformed, and h11 would follow the chunked body past the length checked here.
        raise HTTPException(400, "Send Content-Length or Transfer-Encoding, not both")
    if raw is None:
        if required:
            raise HTTPException(411, "Send the file with its size (Content-Length)")
        return None
    try:
        n = int(raw)
    except ValueError:
        raise HTTPException(400, "Bad Content-Length")
    if n < 0:
        raise HTTPException(400, "Bad Content-Length")
    if n > cap:
        raise HTTPException(413, too_big)
    return n


async def read_capped(request: Request, cap: int, *, too_big: str = "Body too large") -> bytes:
    """The raw body, read in chunks and stopped at *cap* whatever the header said."""
    check_declared(request, cap, too_big=too_big)
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > cap:
            raise HTTPException(413, too_big)
        chunks.append(chunk)
    return b"".join(chunks)
