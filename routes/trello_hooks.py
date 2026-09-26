"""Trello's webhook callback: ``HEAD``/``POST /hooks/trello`` (spec 006, research R3).

⚠ **This is PawPoller's one unauthenticated route that takes a body from the
internet.** It is deliberately outside ``/api/trello`` (which is loopback-only on
an instance with no password) and in ``dashboard._AUTH_EXEMPT_PATHS``. What
stands in for a login:

* the ``X-Trello-Webhook`` signature — base64 HMAC-SHA1 of the raw body plus the
  callback URL, keyed with the app Secret. A bad or missing signature is 401;
* a size cap and a per-address rate limit;
* the payload can only *mark boards dirty*. It never writes a value: the poller
  reads the board from Trello with our own credentials, so a forged delivery
  that somehow passed could make us re-read a board early, and nothing else.

Nothing from the body is logged — it carries card text and member names.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import threading
import time

from fastapi import APIRouter, Request, Response

from clients.trello.client import verify_webhook
from trello import mapping, runtime, webhooks

logger = logging.getLogger(__name__)

hooks_router = APIRouter(tags=["trello-hooks"])

MAX_BODY = 256 * 1024
RATE_PER_MINUTE = 120
_hits: dict = {}
_hits_lock = threading.Lock()


def _client_ip(request: Request) -> str:
    """The caller's address. ``cf-connecting-ip`` is only believed when the
    direct peer is local or private (Caddy in front of the container) — a caller
    reaching the origin directly could otherwise set it to anything."""
    peer = request.client.host if request.client else ""
    try:
        trusted = ipaddress.ip_address(peer).is_private or ipaddress.ip_address(peer).is_loopback
    except ValueError:
        trusted = False
    return (request.headers.get("cf-connecting-ip") if trusted else "") or peer


def _limited(ip: str) -> bool:
    now = time.monotonic()
    with _hits_lock:
        recent = [t for t in _hits.get(ip, []) if now - t < 60]
        recent.append(now)
        _hits[ip] = recent
        if len(_hits) > 2000:
            # Drop idle addresses, never the table: clearing it would reset
            # every caller's count at once.
            for k in [k for k, v in _hits.items() if not v or now - v[-1] >= 60]:
                del _hits[k]
            while len(_hits) > 2000:
                del _hits[next(iter(_hits))]
        return len(recent) > RATE_PER_MINUTE


_last_refusal_log = 0.0


def _log_refusal() -> None:
    """At most one line a minute — refusals are the one thing a stranger can
    make this route do, and the log should not be theirs to fill."""
    global _last_refusal_log
    now = time.monotonic()
    if now - _last_refusal_log >= 60:
        _last_refusal_log = now
        logger.warning("Trello webhook refused: bad or missing signature.")


def _signed(body: bytes, callback: str, header: str) -> bool:
    """Trello's docs sign ``JSON.stringify(body)``; that is normally the raw body
    byte for byte, but a compact re-serialisation is tried too so a whitespace
    difference upstream cannot silently switch live updates off (research R3)."""
    secret = mapping.secret()
    if verify_webhook(body, callback, secret, header):
        return True
    try:
        compact = json.dumps(json.loads(body), separators=(",", ":"),
                             ensure_ascii=False).encode("utf-8")
    except ValueError:
        return False
    return compact != body and verify_webhook(compact, callback, secret, header)


@hooks_router.head("/hooks/trello")
def trello_hook_head():
    """Trello's creation check — it must get a 200 or it will not make the webhook."""
    return Response(status_code=200)


@hooks_router.post("/hooks/trello")
async def trello_hook(request: Request):
    if _limited(_client_ip(request)):
        return Response(status_code=429)
    try:
        declared = int(request.headers.get("content-length") or 0)
    except ValueError:
        declared = 0
    if declared > MAX_BODY:
        return Response(status_code=413)
    # Read with a cap: Content-Length can be absent (chunked) or wrong.
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY:
            return Response(status_code=413)
        chunks.append(chunk)
    body = b"".join(chunks)
    callback = mapping.get_config()["webhook_callback"]
    if not (callback and _signed(body, callback, request.headers.get("x-trello-webhook", ""))):
        _log_refusal()
        return Response(status_code=401)
    try:
        payload = json.loads(body)
    except ValueError:
        # Signed but unreadable: 200 so Trello does not retry a body it will
        # resend identically; the safety sweep catches whatever it was about.
        return Response(status_code=200)
    if isinstance(payload, dict):
        webhooks.handle(payload, runtime)
    return Response(status_code=200)
