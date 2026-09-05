"""Tech Centre routes (4.10.0) — ``/api/tech/*``.

The SPA's side of ``techcentre.py``: the consent switch, the first-error prompt,
the Diagnostics panel's status, a test report, and the sink for uncaught
browser errors. All auth-gated like every other ``/api`` route.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request

import techcentre

logger = logging.getLogger(__name__)
tech_router = APIRouter(prefix="/api/tech", tags=["tech"])


@tech_router.get("/status")
def tech_status():
    return techcentre.status()


@tech_router.post("/consent")
def tech_consent(body: dict):
    if "value" not in body:
        raise HTTPException(400, "value (true/false) required")
    techcentre.set_consent(bool(body["value"]))
    return techcentre.status()


@tech_router.post("/prompt")
def tech_prompt(body: dict):
    decision = str(body.get("decision") or "").strip().lower()
    if decision not in ("always", "once", "never"):
        raise HTTPException(400, "decision must be always, once or never")
    out = techcentre.resolve_prompt(decision)
    out["status"] = techcentre.status()
    return out


@tech_router.post("/frontend-error")
def tech_frontend_error(body: dict, request: Request):
    """``window.onerror`` / ``unhandledrejection`` from the SPA. Same consent, same queue."""
    message = str(body.get("message") or "")[:500]
    if not message:
        raise HTTPException(400, "message required")
    source = str(body.get("source") or "")[:120]
    line = body.get("line")
    where = f"frontend:{source.rsplit('/', 1)[-1]}:{line}" if source else "frontend"
    ua = request.headers.get("user-agent", "")[:60]
    outcome = techcentre.report(
        "frontend", where, str(body.get("error_class") or "JSError")[:80], message,
        tb=str(body.get("stack") or "")[:4096],
        context={"page": str(body.get("page") or "")[:80], "browser": ua},
    )
    return {"outcome": outcome}


@tech_router.post("/test")
def tech_test():
    if not techcentre.TECH_CENTRE_URL:
        raise HTTPException(400, "Tech Centre reporting is disabled in this build")
    res = techcentre.test_report()
    if not res.get("ok"):
        raise HTTPException(502, f"The Tech Centre did not accept the report ({res.get('status')}: {res.get('error')})")
    return res


@tech_router.post("/sample")
def tech_sample(body: dict):
    """Send one deliberately fake report of the given kind — Settings → Diagnostics → *Send a sample*."""
    if not techcentre.TECH_CENTRE_URL:
        raise HTTPException(400, "Tech Centre reporting is disabled in this build")
    kind = str(body.get("kind") or "").strip().lower()
    if kind not in techcentre.SAMPLES:
        raise HTTPException(400, f"kind must be one of: {', '.join(techcentre.SAMPLES)}")
    res = techcentre.send_sample(kind)
    if not res.get("ok"):
        raise HTTPException(502, f"The Tech Centre did not accept the sample ({res.get('status')}: {res.get('error')})")
    return res


@tech_router.post("/flush")
def tech_flush():
    return {"sent": techcentre.flush(), "status": techcentre.status()}


@tech_router.get("/known/{fingerprint}")
def tech_known(fingerprint: str):
    k = techcentre.refresh_known(fingerprint)
    if k is None:
        raise HTTPException(404, "Not a known issue")
    return k
