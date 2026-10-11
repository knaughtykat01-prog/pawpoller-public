"""Terms + Privacy acceptance routes (4.67.0, spec 033) — ``/api/legal/*``.

Reachable by a signed-in session even before acceptance (the middleware's Terms gate
lets ``/api/legal/`` through), so the sign-up screen can record it.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

import legal

logger = logging.getLogger(__name__)
legal_router = APIRouter(prefix="/api/legal", tags=["legal"])


@legal_router.get("/status")
def legal_status():
    return legal.status()


@legal_router.post("/accept")
def legal_accept(body: dict):
    try:
        rec = legal.accept(body.get("terms_version") or 0, body.get("privacy_version") or 0)
    except (TypeError, ValueError):
        raise HTTPException(409, "These aren't the current Terms. Reload the page to see the latest version.")
    logger.info("Legal: terms v%s + privacy v%s accepted", rec["terms"], rec["privacy"])
    return {"accepted": rec}
