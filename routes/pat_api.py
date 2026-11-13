"""REST API for Patreon (pat) — spec 084, contracts/pat-api.md.

The session comes from the in-app sign-in (``auth/browser_login.py``); there is no paste route. Every route takes a
Patreon account (``account_id``; none = the default) and refuses any other platform's account. Writes refuse a
never-post account. Tier and collection titles go out as plain text; the page escapes them. A bot check answers 429
and a Patreon error 424, each with its reason (the shared handler blanks every 5xx), never "signed out".
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query

import config
from clients.pat import account as pat_account
from clients.pat.client import PatAuthError, PatChallenged, PatClient, PatUnavailable
from database import pat_queries
from database.db import get_connection
from polling.background import spawn_poll
from polling.pat_poller import pat_poll_progress, run_pat_poll_cycle

logger = logging.getLogger(__name__)
pat_router = APIRouter(prefix="/api/pat")


def _account(account_id: int | None, *, write: bool = False) -> tuple[int, bool]:
    """(account id, is_default) for a Patreon account; 404 for any other platform's, or a never-post one on a write."""
    from database import accounts as adb
    conn = get_connection()
    try:
        aid = int(account_id) if account_id else adb.get_default_account_id(conn, "pat", create=True)
        acct = adb.get_account(conn, aid) if aid is not None else None if aid is not None else None
    finally:
        conn.close()
    if aid is None or not acct or acct["platform"] != "pat" or (write and aid in adb.never_post_ids()):
        raise HTTPException(404, "Not a Patreon account PawPoller may use")
    return aid, bool(acct["is_default"])


def _cookie(aid: int, is_default: bool) -> str:
    return (config.resolve_account_credentials("pat", aid, is_default).get("pat_cookie") or "").strip()


async def _read(aid: int, is_default: bool, fn):
    cookie = _cookie(aid, is_default)
    if not cookie:
        raise HTTPException(409, "Patreon isn't connected (Accounts → Patreon → Sign in).")
    saved = pat_account.values(aid)["campaign_id"]
    if not saved:      # only the sign-in check records the page; never adopt whichever page this cookie is for
        raise HTTPException(409, "Sign in to Patreon again: PawPoller doesn't know which page this is.")
    from polling import fa_standdown
    if fa_standdown.remaining("pat") > 0:
        from clients.pat.client import PAUSED
        raise HTTPException(429, PAUSED.format(when=fa_standdown.next_try_text('pat')))
    try:
        async with PatClient(cookie) as c:
            camp = await c.current_campaign()
            if camp["campaign_id"] != saved:
                raise HTTPException(409, "This Patreon sign-in is for a different page than the saved one. Sign in again.")
            return await fn(c, camp)
    except PatChallenged as e:
        raise HTTPException(429, str(e))
    except PatAuthError as e:
        raise HTTPException(401, str(e))
    except PatUnavailable as e:
        raise HTTPException(424, str(e))


@pat_router.get("/auth/status")
def pat_auth_status(account_id: int | None = Query(None)):
    aid, is_default = _account(account_id)
    vals = pat_account.values(aid)
    s = config.get_settings()
    return {"signed_in": bool(_cookie(aid, is_default)), "campaign_name": s.get(
                config.account_setting_key(aid, "pat_username", is_default), ""),
            "has_campaign": bool(vals["campaign_id"]), "is_nsfw": vals["is_nsfw"],
            "default_audience": vals["default_audience"],
            "note": "Patreon has no post API: PawPoller uses your browser sign-in. Patreon shows no view counts."}


@pat_router.get("/tiers")
async def pat_tiers(account_id: int | None = Query(None)):
    """Read fresh from Patreon and saved (``pat_tiers`` / ``pat_is_nsfw``): the only write path for them."""
    aid, is_default = _account(account_id)

    async def _get(c, camp):
        rules = await c.audience_rules(camp["campaign_id"])
        pat_account.record_campaign(aid, camp, rules)
        return {"is_nsfw": camp["is_nsfw"], "currency": camp["currency"],
                "rules": [{"id": r["rule_id"], "kind": r["kind"], "tier_id": r["tier_id"], "title": r["title"],
                           "amount_cents": r["amount_cents"], "free": r["kind"] == "tier" and not r["amount_cents"]}
                          for r in rules]}
    return await _read(aid, is_default, _get)


@pat_router.put("/default-audience")
def pat_default_audience(body: dict):
    aid, _ = _account(body.get("account_id"), write=True)
    audience = str(body.get("audience") or "")
    if audience not in ("public", "patrons", "tiers"):
        raise HTTPException(400, "audience must be public, patrons or tiers")
    tier_ids = body.get("tier_ids") or []
    if not isinstance(tier_ids, list) or len(tier_ids) > 50:
        raise HTTPException(400, "tier_ids must be a list of up to 50 tier ids")
    known = {r["tier_id"] for r in pat_account.values(aid)["tiers"] if r.get("kind") == "tier"}
    if any(str(t) not in known for t in tier_ids):
        raise HTTPException(400, "One of those tiers isn't on this Patreon page. Refresh the tier list.")
    pat_account.save(aid, default_audience={"audience": audience, "tier_ids": [str(t) for t in tier_ids],
                                            "notify": body.get("notify", True) is not False})
    return {"ok": True}


@pat_router.get("/collections")
async def pat_collections(account_id: int | None = Query(None)):
    aid, is_default = _account(account_id)

    async def _get(c, camp):
        return {"collections": await c.collections(camp["campaign_id"])}
    return await _read(aid, is_default, _get)


@pat_router.post("/auth/disconnect")
def pat_disconnect(account_id: int | None = Query(None)):
    aid, _ = _account(account_id)
    gone = pat_account.clear(aid)
    return {"status": "success", "message": "Patreon disconnected", "cleared": len(gone)}


# ── Polling and data ─────────────────────────────────────────────────────────

@pat_router.get("/poll/progress")
def get_pat_poll_progress():
    return dict(pat_poll_progress)


@pat_router.post("/poll/trigger")
async def trigger_pat_poll(account_id: int | None = Query(None)):
    aid, _ = _account(account_id)
    spawn_poll(run_pat_poll_cycle(aid), "run_pat_poll_cycle")
    return {"status": "started"}


@pat_router.get("/status")
def get_pat_status():
    conn = get_connection()
    try:
        return {"total_submissions": conn.execute("SELECT COUNT(*) AS c FROM pat_submissions").fetchone()["c"],
                "total_snapshots": conn.execute("SELECT COUNT(*) AS c FROM pat_snapshots").fetchone()["c"],
                "last_poll": pat_queries.get_pat_last_poll(conn)}
    finally:
        conn.close()


@pat_router.get("/summary")
def get_pat_summary(account_id: int | None = Query(None)):
    conn = get_connection()
    try:
        return pat_queries.get_pat_summary(conn, account_id=account_id)
    finally:
        conn.close()


@pat_router.get("/submissions")
def get_pat_submissions(sort_by: str = Query("favorites_count"), order: str = Query("desc"),
                        account_id: int | None = Query(None)):
    conn = get_connection()
    try:
        return {"submissions": pat_queries.get_all_pat_submissions(conn, sort_by, order, account_id=account_id)}
    finally:
        conn.close()


@pat_router.get("/submissions/{submission_id}/snapshots")
def get_pat_submission_snapshots(submission_id: str, start: str | None = Query(None), end: str | None = Query(None)):
    conn = get_connection()
    try:
        return {"snapshots": pat_queries.get_pat_snapshots(conn, submission_id, start, end)}
    finally:
        conn.close()


@pat_router.get("/poll_log")
def get_pat_poll_log(limit: int = Query(50, ge=1, le=200)):
    conn = get_connection()
    try:
        return {"polls": pat_queries.get_pat_poll_log(conn, limit)}
    finally:
        conn.close()
