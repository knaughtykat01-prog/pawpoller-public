"""Facebook Page connect routes (spec 022, 4.57.0) — /api/fb/*.

Connecting is two steps so a person with several Pages can choose:
  1. ``POST /auth/pages`` — the Graph API Explorer token (+ App ID + App secret) is turned
     into a long-lived user token server-side and the Pages it can post to are listed.
     The Page tokens stay on the server, held for ten minutes under a one-time ``pick``
     id; the browser only ever sees names and ids. The app secret is used for that one
     call and never stored or logged.
  2. ``POST /auth/connect`` — ``{pick, page_id, account_id?}`` saves that Page's token.
"""
from __future__ import annotations

import logging
import secrets
import time

from fastapi import APIRouter, HTTPException, Query

import config
from database.db import get_connection

logger = logging.getLogger(__name__)
fb_router = APIRouter(prefix="/api/fb")

_PICK_TTL = 600
_picks: dict[str, tuple[float, list[dict]]] = {}     # ponytail: in-process; a restart just means "find your Pages again"


def _account(account_id) -> tuple[int | None, bool]:
    """(account_id, is_default) — None means the platform default's bare keys."""
    if not account_id:
        return None, True
    from database import accounts as adb
    conn = get_connection()
    try:
        acct = adb.get_account(conn, int(account_id))
    finally:
        conn.close()
    if not acct or acct["platform"] != "fb":
        raise HTTPException(404, f"No Facebook account {account_id}")
    return int(account_id), bool(acct["is_default"])


def _key(aid: int | None, is_default: bool, field: str) -> str:
    return field if aid is None else config.account_setting_key(aid, field, is_default)


def _sweep() -> None:
    now = time.time()
    for k in [k for k, (t, _) in _picks.items() if now - t > _PICK_TTL]:
        _picks.pop(k, None)


@fb_router.get("/auth/status")
def fb_auth_status(account_id: int | None = Query(None)):
    aid, is_default = _account(account_id)
    s = config.get_settings()
    return {"has_credentials": bool(s.get(_key(aid, is_default, "fb_page_token")) and s.get(_key(aid, is_default, "fb_page_id"))),
            "username": s.get(_key(aid, is_default, "fb_page_name"), "") or s.get(_key(aid, is_default, "fb_page_id"), "")}


@fb_router.post("/auth/pages")
async def fb_find_pages(body: dict):
    """Exchange the pasted token and list the Pages it can post to (names + ids only)."""
    from clients.fb.client import FbClient, FbError
    token = str(body.get("user_token", "") or "").strip()
    app_id = str(body.get("app_id", "") or "").strip()
    app_secret = str(body.get("app_secret", "") or "").strip()
    if not token:
        raise HTTPException(400, "Paste the token from Graph API Explorer first.")
    if bool(app_id) != bool(app_secret):
        raise HTTPException(400, "Fill in both the App ID and the App secret (or leave both empty if you pasted a Page token).")
    async with FbClient() as client:
        try:
            if app_id:
                token = await client.exchange_token(token, app_id, app_secret)
            pages = await client.list_pages(token)
        except FbError as e:
            raise HTTPException(400, str(e))
    pages = [p for p in pages if p.get("id") and p.get("access_token")]
    if not pages:
        raise HTTPException(400, "That token doesn't manage any Facebook Pages. Make the token while logged in as a "
                                 "Page admin, and choose the Page when Facebook asks which ones to allow.")
    _sweep()
    pick = secrets.token_urlsafe(16)
    _picks[pick] = (time.time(), pages)
    return {"pick": pick, "pages": [{"id": p["id"], "name": p["name"], "can_post": p["can_post"]} for p in pages],
            "long_lived": bool(app_id)}


@fb_router.post("/auth/connect")
async def fb_connect(body: dict):
    """Save the chosen Page's token for this account, after checking it reads the Page."""
    from clients.fb.client import FbClient
    _sweep()
    entry = _picks.get(str(body.get("pick", "")))
    if not entry:
        raise HTTPException(400, "That list of Pages has expired. Press Find my Pages again.")
    page = next((p for p in entry[1] if p["id"] == str(body.get("page_id", ""))), None)
    if not page:
        raise HTTPException(400, "Choose one of the Pages in the list.")
    aid, is_default = _account(body.get("account_id"))
    async with FbClient(page_token=page["access_token"], page_id=page["id"]) as client:
        name = await client.validate_session()
    if not name:
        raise HTTPException(400, "Facebook didn't accept that Page's token. Make a new token and try again.")
    config.save_settings({_key(aid, is_default, "fb_page_token"): page["access_token"],
                          _key(aid, is_default, "fb_page_id"): page["id"],
                          _key(aid, is_default, "fb_page_name"): name})
    _picks.pop(str(body.get("pick", "")), None)
    _sync_handle(aid, name)
    logger.info("Facebook Page connected (account=%s)", aid)
    return {"status": "success", "message": f"Connected — posting to {name}", "page": name}


@fb_router.post("/auth/disconnect")
def fb_disconnect(body: dict | None = None):
    aid, is_default = _account((body or {}).get("account_id"))
    config.delete_settings_keys([_key(aid, is_default, f) for f in ("fb_page_token", "fb_page_id", "fb_page_name")])
    return {"status": "success", "message": "Facebook disconnected"}


def _sync_handle(aid: int | None, handle: str) -> None:
    """The account row's handle follows the Page name, so the Accounts page names it."""
    from database import accounts as adb
    conn = get_connection()
    try:
        adb.ensure_accounts_table(conn)
        target = aid if aid is not None else adb.get_default_account_id(conn, "fb", create=True)
        if target is not None:
            conn.execute("UPDATE accounts SET handle = ? WHERE account_id = ?", (handle, target))
            conn.commit()
    except Exception:
        logger.debug("Facebook: could not sync the account handle", exc_info=True)
    finally:
        conn.close()

# ── Stats (spec 029, 4.59.0) ─────────────────────────────────────────────────

@fb_router.get("/poll/progress")
def get_fb_poll_progress():
    from polling.fb_poller import fb_poll_progress
    return dict(fb_poll_progress)


@fb_router.post("/poll/trigger")
async def trigger_fb_poll(account_id: int | None = Query(None)):
    from polling.background import spawn_poll
    from polling.fb_poller import run_fb_poll_cycle
    spawn_poll(run_fb_poll_cycle(account_id), "run_fb_poll_cycle")
    return {"status": "started"}


@fb_router.post("/poll/full-resync")
async def fb_full_resync(account_id: int | None = Query(None)):
    from polling.background import spawn_poll
    from polling.fb_poller import run_fb_poll_cycle
    spawn_poll(run_fb_poll_cycle(account_id, force_full=True), "run_fb_poll_cycle full-resync")
    return {"status": "started"}


@fb_router.get("/status")
def get_fb_status():
    from database import fb_queries
    conn = get_connection()
    try:
        return {"total_submissions": conn.execute("SELECT COUNT(*) AS c FROM fb_submissions").fetchone()["c"],
                "total_snapshots": conn.execute("SELECT COUNT(*) AS c FROM fb_snapshots").fetchone()["c"],
                "last_poll": fb_queries.get_fb_last_poll(conn)}
    finally:
        conn.close()


@fb_router.get("/summary")
def get_fb_summary(account_id: int | None = Query(None)):
    """Totals (None = Facebook gave no figure, shown as "not available") + the permission the last
    poll was refused, so the dashboard can say what to add."""
    from database import fb_queries
    conn = get_connection()
    try:
        out = fb_queries.get_summary(conn, account_id=account_id)
        last = fb_queries.get_fb_last_poll(conn)
        out["last_poll"] = last["finished_at"] if last else None
        out["missing_permission"] = (last or {}).get("missing_permission") or ""
        return out
    finally:
        conn.close()


@fb_router.get("/submissions")
def get_fb_submissions(sort_by: str = Query("posted_at"), order: str = Query("desc"), search: str = Query(""),
                       account_id: int | None = Query(None)):
    from database import fb_queries
    conn = get_connection()
    try:
        subs = fb_queries.get_all_posts(conn, sort_by=sort_by, order=order, account_id=account_id)
    finally:
        conn.close()
    if search:
        sl = search.lower()
        subs = [p for p in subs if sl in (p["full_text"] or "").lower()]
    return {"submissions": subs, "total": len(subs)}


@fb_router.get("/submissions/{submission_id}/snapshots")
def get_fb_submission_snapshots(submission_id: str):
    from database import fb_queries
    conn = get_connection()
    try:
        return {"snapshots": fb_queries.get_post_snapshots(conn, submission_id)}
    finally:
        conn.close()


@fb_router.get("/submissions/{submission_id}")
def get_fb_submission(submission_id: str):
    from database import fb_queries
    conn = get_connection()
    try:
        sub = fb_queries.get_post(conn, submission_id)
        if not sub:
            raise HTTPException(404, "Facebook post not found")
        return {"submission": sub}
    finally:
        conn.close()


@fb_router.get("/aggregate")
def get_fb_aggregate(start: str | None = Query(None), end: str | None = Query(None),
                     account_id: int | None = Query(None)):
    from database import fb_queries
    conn = get_connection()
    try:
        return {"snapshots": fb_queries.get_aggregate(conn, start, end, account_id=account_id)}
    finally:
        conn.close()


@fb_router.get("/poll_log")
def get_fb_poll_log(limit: int = Query(50, ge=1, le=200)):
    from database import fb_queries
    conn = get_connection()
    try:
        return {"polls": fb_queries.get_poll_log(conn, limit)}
    finally:
        conn.close()
