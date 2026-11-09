"""Tech Centre routes (4.10.0) — ``/api/tech/*``.

The SPA's side of ``techcentre.py``: the consent switch, the first-error prompt,
the Diagnostics panel's status, a test report, and the sink for uncaught
browser errors. All auth-gated like every other ``/api`` route.
"""
from __future__ import annotations

import logging
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request

import techcentre

logger = logging.getLogger(__name__)


def _from_dashboard(request: Request) -> None:
    """Spec 079 (FR-SEC-004): every POST here comes from the dashboard's own page, as JSON.

    Before an account exists the person at the machine is let in on loopback, and seats share a parent domain, so
    another website's form or no-cors fetch could otherwise turn check-ins or error reports on. `none` isn't
    accepted: a POST is never a typed navigation. Same rule as dashboard._app_reset_from_here.
    Residual: a DNS-rebinding page counts as same-origin (BACKLOG SEC079: an app-wide Host allow-list)."""
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    ctype = (request.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
    host = request.headers.get("host") or ""
    site = request.headers.get("sec-fetch-site")
    if site is not None:
        ok = site == "same-origin"
    elif request.headers.get("origin") is not None:
        origin = request.headers["origin"]
        ok = origin != "null" and bool(host) and urlsplit(origin).netloc == host
    else:
        ok = bool(host) and urlsplit(request.headers.get("referer") or "").netloc == host
    if ctype != "application/json" or not ok:
        raise HTTPException(403, "not_from_dashboard")


tech_router = APIRouter(prefix="/api/tech", tags=["tech"], dependencies=[Depends(_from_dashboard)])


@tech_router.get("/status")
def tech_status():
    return techcentre.status()


@tech_router.post("/consent")
def tech_consent(body: dict):
    if "value" not in body:
        raise HTTPException(400, "value (true/false) required")
    techcentre.set_consent(bool(body["value"]))
    return techcentre.status()


@tech_router.post("/usage")
def tech_usage(body: dict):
    """The "count this copy" switch (4.42.0, spec 009) — separate from error reports.

    Spec 079: a JSON true/false only, and where it was given (consent_records.WHERE; missing = an old page)."""
    import consent_records
    value = body.get("value")
    if type(value) is not bool:
        raise HTTPException(400, "value must be true or false")
    where = body.get("where")
    if where is not None and (not isinstance(where, str) or where not in consent_records.WHERE):
        raise HTTPException(400, "bad where")
    try:
        techcentre.set_usage_consent(value, where)
    except techcentre.AlreadyAnswered:
        raise HTTPException(409, "answered")
    return techcentre.status()


@tech_router.post("/usage-ask/shown")
def tech_usage_ask_shown():
    """Spec 079: the ask was shown (counted here so a reload can't reset it). Sends nothing."""
    return {"shown": techcentre.mark_ask_shown()}


@tech_router.get("/checkin-preview")
def tech_checkin_preview():
    """Exactly what a check-in from this install would send (the Diagnostics preview)."""
    return techcentre.checkin_payload()


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
