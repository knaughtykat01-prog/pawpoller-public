"""REST API for Picarto (pic) — spec 013 (4.46.0).

Poll-only and login-free: the one setting is the channel name. ``POST /channel`` saves it
only after Picarto confirms the channel exists, and stores the name as Picarto spells it.
The numbers are channel-level (``pic_channel_snapshots``); recordings are inventory.
"""
from __future__ import annotations

import csv
import io
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

import config
from database import pic_queries
from database.db import get_connection
from polling.background import spawn_poll
from polling.pic_poller import pic_poll_progress, run_pic_poll_cycle

logger = logging.getLogger(__name__)
pic_router = APIRouter(prefix="/api/pic")


def _account(account_id: int | None) -> tuple[int | None, bool]:
    """(account_id, is_default) — None means the platform default's bare keys."""
    if not account_id:
        return None, True
    from database import accounts as adb
    conn = get_connection()
    try:
        acct = adb.get_account(conn, int(account_id))
    finally:
        conn.close()
    if not acct or acct["platform"] != "pic":
        raise HTTPException(404, f"No Picarto account {account_id}")
    return int(account_id), bool(acct["is_default"])


def _key(aid: int | None, is_default: bool) -> str:
    return "pic_channel" if aid is None else config.account_setting_key(aid, "pic_channel", is_default)


# ── Channel (the only setting) ───────────────────────────────────────────────

@pic_router.get("/channel/status")
def pic_channel_status(account_id: int | None = Query(None)):
    aid, is_default = _account(account_id)
    channel = config.get_settings().get(_key(aid, is_default), "") or ""
    conn = get_connection()
    try:
        has_data = conn.execute("SELECT COUNT(*) AS c FROM pic_channel_snapshots").fetchone()["c"] > 0
    finally:
        conn.close()
    return {"has_credentials": bool(channel), "channel": channel, "has_data": has_data}


@pic_router.post("/channel")
async def pic_save_channel(body: dict):
    """Check the channel exists on Picarto, then save it (spelled as Picarto spells it)."""
    from clients.pic.client import PicClient, PicError, clean_channel
    raw = str(body.get("channel", "") or "")
    name = clean_channel(raw)
    if not name:
        raise HTTPException(400, "That isn't a Picarto channel name — it's the last part of your picarto.tv address.")
    aid, is_default = _account(body.get("account_id"))
    client = PicClient(name)
    try:
        ch = await client.get_channel(refresh=True)
    except PicError as e:
        raise HTTPException(502, str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"Couldn't reach Picarto: {type(e).__name__}")
    finally:
        await client.close()
    # Picarto's own spelling, kept only if it is still a clean channel name (4.46.2, release review).
    canonical = clean_channel(ch["name"]) if ch else ""
    if not canonical:
        raise HTTPException(400, f"No Picarto channel called {name}.")
    config.save_settings({_key(aid, is_default): canonical})
    _sync_handle(aid, canonical)
    logger.info("Picarto channel saved (account=%s)", aid)
    return {"status": "success", "channel": canonical, "adult": ch["adult"]}


def _sync_handle(aid: int | None, handle: str) -> None:
    """The account row's handle follows the channel, so the Accounts page names it."""
    from database import accounts as adb
    conn = get_connection()
    try:
        adb.ensure_accounts_table(conn)
        target = aid if aid is not None else adb.get_default_account_id(conn, "pic", create=True)
        if target is not None:
            conn.execute("UPDATE accounts SET handle = ? WHERE account_id = ?", (handle, target))
            conn.commit()
    except Exception:
        logger.debug("Picarto: could not sync the account handle", exc_info=True)
    finally:
        conn.close()


# ── Polling ──────────────────────────────────────────────────────────────────

@pic_router.get("/poll/progress")
def get_pic_poll_progress():
    return dict(pic_poll_progress)


@pic_router.post("/poll/trigger")
async def trigger_pic_poll():
    spawn_poll(run_pic_poll_cycle(), "run_pic_poll_cycle")
    return {"status": "started"}


@pic_router.post("/poll/full-resync")
async def pic_full_resync():
    spawn_poll(run_pic_poll_cycle(force_full=True), "run_pic_poll_cycle full-resync")
    return {"status": "started"}


# ── Data ─────────────────────────────────────────────────────────────────────

@pic_router.get("/status")
def get_pic_status():
    conn = get_connection()
    try:
        return {
            "total_submissions": conn.execute("SELECT COUNT(*) AS c FROM pic_submissions").fetchone()["c"],
            "total_snapshots": conn.execute("SELECT COUNT(*) AS c FROM pic_channel_snapshots").fetchone()["c"],
            "last_poll": pic_queries.get_pic_last_poll(conn),
        }
    finally:
        conn.close()


@pic_router.get("/summary")
def get_pic_summary(account_id: int | None = Query(None)):
    conn = get_connection()
    try:
        summary = pic_queries.get_pic_summary(conn, account_id=account_id)
        summary["growth_rates"] = pic_queries.get_pic_growth_rates(conn, account_id=account_id)
        return summary
    finally:
        conn.close()


@pic_router.get("/submissions")
def get_pic_submissions(sort_by: str = Query("posted_at"), order: str = Query("desc"), search: str = Query(""),
                        account_id: int | None = Query(None)):
    conn = get_connection()
    try:
        subs = pic_queries.get_all_pic_recordings(conn, sort_by=sort_by, order=order, account_id=account_id)
    finally:
        conn.close()
    if search:
        sl = search.lower()
        subs = [s for s in subs if sl in (s["title"] or "").lower()]
    return {"submissions": subs, "total": len(subs)}


@pic_router.get("/submissions/{submission_id}/snapshots")
def get_pic_submission_snapshots(submission_id: str):
    """The per-submission series every platform serves. Picarto recordings carry no stats (views are
    always 0), so phase 1 writes none — an empty series, not a 404. Declared before the detail route
    so the path isn't swallowed by it."""
    conn = get_connection()
    try:
        return {"snapshots": [dict(r) for r in conn.execute(
            "SELECT * FROM pic_snapshots WHERE submission_id = ? ORDER BY polled_at", (submission_id,)).fetchall()]}
    finally:
        conn.close()


@pic_router.get("/submissions/{submission_id}")
def get_pic_submission(submission_id: str):
    conn = get_connection()
    try:
        sub = pic_queries.get_pic_recording(conn, submission_id)
        if not sub:
            raise HTTPException(404, "Picarto recording not found")
        try:
            tags = conn.execute(
                "SELECT t.tag_id, t.name, t.color FROM tags t JOIN submission_tags st "
                "ON t.tag_id = st.tag_id WHERE st.platform = 'pic' AND st.submission_id = ?",
                (submission_id,)).fetchall()
        except Exception:
            tags = []
        sub["tags"] = [dict(r) for r in tags]
        return {"submission": sub}
    finally:
        conn.close()


@pic_router.get("/aggregate")
def get_pic_aggregate(start: Optional[str] = Query(None), end: Optional[str] = Query(None),
                      account_id: int | None = Query(None)):
    conn = get_connection()
    try:
        return {"snapshots": pic_queries.get_pic_aggregate(conn, start, end, account_id=account_id)}
    finally:
        conn.close()


@pic_router.get("/poll_log")
def get_pic_poll_log(limit: int = Query(50, ge=1, le=200)):
    conn = get_connection()
    try:
        return {"polls": pic_queries.get_pic_poll_log(conn, limit)}
    finally:
        conn.close()


# ── CSV export ───────────────────────────────────────────────────────────────

def _cell(val):
    """Neutralise spreadsheet formulas in exported text."""
    if isinstance(val, str) and val and val[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + val
    return val


def _csv(rows: list[dict], filename: str) -> StreamingResponse:
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    if not rows:
        return StreamingResponse(iter(["No data"]), media_type="text/csv", headers=headers)
    out = io.StringIO()
    w = csv.DictWriter(out, fieldnames=rows[0].keys())
    w.writeheader()
    w.writerows({k: _cell(v) for k, v in r.items()} for r in rows)
    return StreamingResponse(iter([out.getvalue()]), media_type="text/csv", headers=headers)


@pic_router.get("/export/submissions")
def export_pic_submissions():
    conn = get_connection()
    try:
        return _csv(pic_queries.get_all_pic_recordings(conn), "picarto_recordings.csv")
    finally:
        conn.close()


@pic_router.get("/export/snapshots")
def export_pic_snapshots():
    conn = get_connection()
    try:
        return _csv(pic_queries.get_pic_channel_snapshots(conn), "picarto_channel.csv")
    finally:
        conn.close()
