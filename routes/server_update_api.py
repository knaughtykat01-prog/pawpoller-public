"""Server-update API — the dashboard "Update now" button and its host-agent handoff (4.32.0).

A container cannot rebuild and replace itself, so these endpoints do NOT run docker/git.
The button records a single-shot request; a host-side systemd agent (server-update/)
polls the loopback-only claim endpoint and runs update.sh on the host. See
specs/002-server-update/ and documentation_guide.md.
"""
from __future__ import annotations

import json
import logging
import time

from fastapi import APIRouter, HTTPException, Request

import config

logger = logging.getLogger(__name__)

server_update_router = APIRouter(prefix="/api/server", tags=["server-update"])

# State lives on the data volume (survives updates). Both are tiny marker files.
_REQUEST_FILE = config.DATA_DIR / ".update-request"
_AGENT_SEEN_FILE = config.DATA_DIR / ".update-agent-seen"
_REQUEST_TTL = 30 * 60      # a pending request older than this is stale — never fire it
_AGENT_FRESH = 10 * 60      # the host agent counts as installed if it polled within this


def _is_server() -> bool:
    """Server-update applies to a server install; the desktop app has its own self-updater."""
    try:
        from posting.scheduler import detect_runtime_mode
        return detect_runtime_mode() == "server"
    except Exception:
        return False


def _agent_seen_ago() -> float | None:
    try:
        return time.time() - _AGENT_SEEN_FILE.stat().st_mtime
    except OSError:
        return None


@server_update_router.get("/update-status")
async def update_status() -> dict:
    """Current version, whether a newer one is published, and whether the host agent is installed."""
    applicable = _is_server()
    latest = None
    available = False
    if applicable:
        try:
            import updater
            info = updater.check_for_update()
            latest = info.get("latest")
            available = bool(info.get("available"))
        except Exception as e:  # offline / rate-limited — report "couldn't check", don't fail
            logger.debug("update-status: release check failed: %s", e)
    seen = _agent_seen_ago()
    return {
        "applicable": applicable,
        "current": config.APP_VERSION,
        "latest": latest,
        "available": available,
        "host_agent_installed": seen is not None and seen < _AGENT_FRESH,
        "in_progress": _REQUEST_FILE.exists(),
    }


@server_update_router.post("/update")
async def request_update() -> dict:
    """Ask for an update (the "Update now" button). Records a request for the host agent to pick up.

    Admin-gated by the session-auth middleware (this path is not auth-exempt).
    """
    if not _is_server():
        raise HTTPException(400, "Server updates apply to a server install only.")
    try:
        _REQUEST_FILE.write_text(json.dumps({"requested_at": int(time.time())}), encoding="utf-8")
    except OSError as e:
        raise HTTPException(500, f"Could not record the update request: {e}")
    seen = _agent_seen_ago()
    installed = seen is not None and seen < _AGENT_FRESH
    return {"status": "requested", "in_progress": True, "host_agent_installed": installed}


@server_update_router.post("/update-claim")
async def claim_update(request: Request) -> dict:
    """Claim a pending request (host agent only). Loopback-gated — a remote client cannot trigger an update.

    Auth-exempt in the middleware (the agent has no cookie); the loopback check IS the gate.
    Atomically records the agent heartbeat and returns+clears any pending request.
    """
    host = request.client.host if request.client else ""
    if host not in ("127.0.0.1", "::1", "localhost"):
        raise HTTPException(403, "This endpoint is reachable only from the server's own host.")

    try:
        _AGENT_SEEN_FILE.write_text(str(int(time.time())), encoding="utf-8")
    except OSError:
        pass

    requested = False
    if _REQUEST_FILE.exists():
        try:
            data = json.loads(_REQUEST_FILE.read_text(encoding="utf-8"))
            requested = (time.time() - float(data.get("requested_at", 0))) < _REQUEST_TTL
        except (OSError, ValueError):
            requested = True  # present but unreadable → honour it once, then clear
        try:
            _REQUEST_FILE.unlink()
        except OSError:
            pass
    return {"requested": requested}
