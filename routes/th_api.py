"""REST API endpoints for Toyhouse (th) — spec 062. The Newgrounds routes, renamed, plus
``GET /characters`` for the post form's character picker.

Cookie session (browser sign-in or a pasted cookie string) + the operator's Toyhouse username; the site has
no API. ``auth/connect`` refuses a cookie that is signed in as someone else (FR-SEC-001). A bot check is a
503 that says so, never "signed out" (FR-SEC-008).
"""
from __future__ import annotations

import re
import csv
import io
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

import config
from database import th_queries
from database.db import get_connection
from database.queries import thin_series
from polling.background import spawn_poll
from polling.th_poller import run_th_poll_cycle, th_poll_progress

logger = logging.getLogger(__name__)
th_router = APIRouter(prefix="/api/th")

CRED_FIELDS = ("th_username", "th_cookie")


def _account(account_id: int | None) -> tuple[int | None, bool]:
    if not account_id:
        return None, True
    from database import accounts as adb
    conn = get_connection()
    try:
        acct = adb.get_account(conn, int(account_id))
    finally:
        conn.close()
    if not acct:
        raise HTTPException(404, f"No account {account_id}")
    return int(account_id), bool(acct["is_default"])


def _creds(account_id: int | None) -> dict:
    settings = config.get_settings()
    aid, is_default = _account(account_id)
    if aid is None:
        return {k: settings.get(k, "") for k in CRED_FIELDS}
    return config.resolve_account_credentials("th", aid, is_default, settings)


def _key(field: str, account_id: int | None, is_default: bool) -> str:
    return field if account_id is None else config.account_setting_key(account_id, field, is_default)


def _check_postable(aid: int | None) -> None:
    """Refuse an account that isn't Toyhouse, or is on the never-post list. None = the default account,
    which is checked too (FR-SEC-007)."""
    from database import accounts as adb
    conn = get_connection()
    try:
        if aid is None:
            aid = adb.get_default_account_id(conn, "th")
        acct = adb.get_account(conn, aid) if aid is not None else None
    finally:
        conn.close()
    if aid is not None and (not acct or acct["platform"] != "th" or aid in adb.never_post_ids()):
        raise HTTPException(404, "Not a Toyhouse account PawPoller may use")


# ── Auth ─────────────────────────────────────────────────────────────────────

@th_router.get("/auth/status")
def th_auth_status(account_id: int | None = Query(None)):
    creds = _creds(account_id)
    has_credentials = bool((creds.get("th_cookie") or "").strip())
    has_data = False
    conn = get_connection()
    try:
        has_data = conn.execute("SELECT COUNT(*) as c FROM th_submissions").fetchone()["c"] > 0
    except Exception:
        pass
    finally:
        conn.close()
    return {"has_credentials": has_credentials, "has_data": has_data,
            "username": (creds.get("th_username") or "").strip(),
            "note": "Toyhouse has no API: PawPoller uses your browser sign-in. Toyhouse shows no view counts."}


@th_router.post("/auth/connect")
async def th_connect(body: dict):
    """Save a pasted cookie string + username after checking whose session it is."""
    username = str(body.get("username", "") or "").strip()
    cookie = str(body.get("cookie", "") or "").strip()
    account_id = body.get("account_id")
    aid, is_default = _account(int(account_id) if account_id else None)
    _check_postable(aid)
    if not cookie:
        raise HTTPException(400, "Paste the Toyhouse cookie string (or use Login via Browser)")
    from clients.th.client import THChallenged, THClient
    client = THClient(username=username, cookie=cookie)
    try:
        session = await client.validate_session()
    except THChallenged as e:
        raise HTTPException(503, str(e))
    except Exception as e:
        raise HTTPException(502, f"Could not reach Toyhouse: {e}")
    finally:
        await client.close()
    if not session.get("logged_in"):
        raise HTTPException(401, session.get("detail") or "That cookie is not signed in to Toyhouse.")
    if not session.get("ok"):
        raise HTTPException(409, session.get("detail") or "That session belongs to a different Toyhouse account.")
    who = session.get("username") or username
    config.save_settings({_key("th_cookie", aid, is_default): cookie, _key("th_username", aid, is_default): who,
                          "th_notifications_enabled": True})
    return {"status": "success", "message": f"Connected — tracking {who}", "username": who}


@th_router.post("/auth/disconnect")
def th_disconnect(account_id: int | None = Query(None)):
    aid, is_default = _account(account_id)
    config.delete_settings_keys([_key(k, aid, is_default) for k in CRED_FIELDS])
    config.save_settings({"th_notifications_enabled": False})
    return {"status": "success", "message": "Toyhouse disconnected"}


@th_router.get("/characters")
async def th_characters(account_id: int | None = Query(None), saved: int = Query(0)):
    """The account's characters for the post form's picker: read live, or the saved list when the site
    can't be asked (stand-down, bot check, network) or ``saved=1`` (the dashboard, which never asks the site).
    Names are plain text; the page escapes them."""
    aid, _ = _account(account_id)
    _check_postable(aid)

    def _shape(rows):
        return [{"id": r["character_id"], "name": r["name"], "favorites_count": r.get("favorites_count", 0),
                 "images_count": r.get("images_count", 0), "comments_count": r.get("comments_count", 0),
                 "read_at": r.get("read_at")}      # None = its page not read yet (the dashboard shows a dash)
                for r in rows]

    def _saved():
        conn = get_connection()
        try:
            return {"characters": _shape(th_queries.get_th_characters(conn, account_id=aid)), "from": "saved"}
        finally:
            conn.close()

    from polling import fa_standdown
    creds = _creds(account_id)
    if saved or fa_standdown.remaining("th") > 0 or not (creds.get("th_cookie") or "").strip():
        return _saved()
    from polling.th_poller import client_from_creds
    client = client_from_creds(creds)
    try:
        return {"characters": _shape(await client.get_characters()), "from": "site"}
    except Exception as e:  # noqa: BLE001 — the saved list is a fine answer
        logger.info("th characters: live read failed (%s), using the saved list", type(e).__name__)
        return _saved()
    finally:
        await client.close()


# ── Polling ──────────────────────────────────────────────────────────────────

@th_router.get("/poll/progress")
def get_th_poll_progress():
    return dict(th_poll_progress)


@th_router.post("/poll/trigger")
async def trigger_th_poll():
    try:
        spawn_poll(run_th_poll_cycle(), "run_th_poll_cycle")
        return {"status": "started"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error in th poll trigger: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))


@th_router.post("/poll/full-resync")
async def th_full_resync():
    try:
        spawn_poll(run_th_poll_cycle(force_full=True), "run_th_poll_cycle full-resync")
        return {"status": "started"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error in th full resync: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))


# ── Data ─────────────────────────────────────────────────────────────────────

@th_router.get("/status")
def get_th_status():
    conn = get_connection()
    try:
        return {
            "total_submissions": conn.execute("SELECT COUNT(*) as c FROM th_submissions").fetchone()["c"],
            "total_snapshots": conn.execute("SELECT COUNT(*) as c FROM th_snapshots").fetchone()["c"],
            "last_poll": th_queries.get_th_last_poll(conn),
        }
    except Exception as e:
        logger.error("Error in /api/th/status: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@th_router.get("/summary")
def get_th_summary(account_id: int | None = Query(None)):
    conn = get_connection()
    try:
        summary = th_queries.get_th_summary(conn, account_id=account_id)
        summary["growth_rates"] = th_queries.get_th_growth_rates(conn)
        return summary
    except Exception as e:
        logger.error("Error in /api/th/summary: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@th_router.get("/submissions")
def get_th_submissions(sort_by: str = Query("favorites_count"), order: str = Query("desc"), search: str = Query(""),
                       account_id: int | None = Query(None)):
    conn = get_connection()
    try:
        subs = th_queries.get_all_th_submissions(conn, sort_by=sort_by, order=order, account_id=account_id)
        deltas = th_queries.get_th_submission_deltas(conn)
        if search:
            sl = search.lower()
            subs = [s for s in subs if sl in s["title"].lower() or sl in (s.get("genre") or "").lower()]
        for s in subs:
            d = deltas.get(s["submission_id"], {})
            s["views_delta"] = d.get("views_delta", 0)
            s["favorites_delta"] = d.get("favorites_delta", 0)
            s["comments_delta"] = d.get("comments_delta", 0)
        return {"submissions": subs, "total": len(subs)}
    except Exception as e:
        logger.error("Error in /api/th/submissions: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@th_router.get("/submissions/{submission_id}/snapshots")
def get_th_submission_snapshots(submission_id: str, start: Optional[str] = Query(None),
                                end: Optional[str] = Query(None)):
    conn = get_connection()
    try:
        return {"snapshots": th_queries.get_th_snapshots(conn, submission_id, start, end)}
    except Exception as e:
        logger.error("Error in /api/th/.../snapshots: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@th_router.get("/submissions/{submission_id}")
def get_th_submission(submission_id: str):
    conn = get_connection()
    try:
        sub = th_queries.get_th_submission(conn, submission_id)
        if not sub:
            raise HTTPException(status_code=404, detail="Toyhouse submission not found")
        try:
            tags = conn.execute(
                "SELECT t.tag_id, t.name, t.color FROM tags t JOIN submission_tags st "
                "ON t.tag_id = st.tag_id WHERE st.platform = 'th' AND st.submission_id = ?",
                (submission_id,)).fetchall()
        except Exception:
            tags = []
        sub_dict = dict(sub)
        sub_dict["tags"] = [dict(r) for r in tags]
        return {"submission": sub_dict, "snapshots": th_queries.get_th_snapshots(conn, submission_id),
                "growth_rates": th_queries.get_th_submission_growth_rates(conn, submission_id)}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error in /api/th/submissions/%s: %s", submission_id[:50], e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@th_router.get("/aggregate/characters")
def get_th_character_aggregate(start: Optional[str] = Query(None), end: Optional[str] = Query(None),
                               account_id: int | None = Query(None)):
    """The dashboard chart (FR-009c): per poll, the account's character favourites and comments."""
    conn = get_connection()
    try:
        return {"snapshots": thin_series(th_queries.get_th_character_totals(conn, start, end, account_id=account_id))}
    finally:
        conn.close()


@th_router.get("/aggregate")
def get_th_aggregate(start: Optional[str] = Query(None), end: Optional[str] = Query(None),
                     account_id: int | None = Query(None)):
    conn = get_connection()
    try:
        return {"snapshots": thin_series(th_queries.get_th_aggregate_snapshots(conn, start, end, account_id=account_id))}
    except Exception as e:
        logger.error("Error in /api/th/aggregate: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@th_router.get("/comparison")
def get_th_comparison(ids: str = Query(...), start: Optional[str] = Query(None), end: Optional[str] = Query(None)):
    conn = get_connection()
    try:
        raw_ids = [x.strip() for x in ids.split(",") if x.strip()]
        if len(raw_ids) > 10:
            raise HTTPException(400, "Max 10 submissions for comparison")
        submission_ids, titles = [], {}
        for rid in raw_ids:
            sub = th_queries.get_th_submission(conn, rid)
            if sub:
                submission_ids.append(sub["submission_id"])
                titles[sub["submission_id"]] = sub["title"]
        return {"series": th_queries.get_th_comparison_snapshots(conn, submission_ids, start, end), "titles": titles}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error in /api/th/comparison: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@th_router.get("/poll_log")
def get_th_poll_log(limit: int = Query(50, ge=1, le=200)):
    conn = get_connection()
    try:
        return {"polls": th_queries.get_th_poll_log(conn, limit)}
    except Exception as e:
        logger.error("Error in /api/th/poll_log: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


# ── CSV export ───────────────────────────────────────────────────────────────

def _sanitize_csv_value(val):
    if isinstance(val, str) and val and val[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + val
    return val


def _csv_response(rows: list[dict], filename: str):
    filename = re.sub(r"[^A-Za-z0-9_.-]", "_", filename)   # SECLOW466: a query id reaches this header
    from fastapi.responses import StreamingResponse
    if not rows:
        return StreamingResponse(iter(["No data"]), media_type="text/csv",
                                 headers={"Content-Disposition": f'attachment; filename="{filename}"'})
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=rows[0].keys())
    writer.writeheader()
    writer.writerows({k: _sanitize_csv_value(v) for k, v in r.items()} for r in rows)
    output.seek(0)
    return StreamingResponse(iter([output.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@th_router.get("/export/submissions")
def export_th_submissions():
    conn = get_connection()
    try:
        return _csv_response(th_queries.get_all_th_submissions(conn), "th_submissions.csv")
    finally:
        conn.close()


@th_router.get("/export/snapshots")
def export_th_snapshots(id: str | None = Query(None)):
    conn = get_connection()
    try:
        if id:
            snaps = th_queries.get_th_snapshots(conn, id)
        else:
            snaps = [dict(r) for r in conn.execute("SELECT * FROM th_snapshots ORDER BY polled_at ASC").fetchall()]
        return _csv_response(snaps, f"th_snapshots{'_' + id[:20] if id else ''}.csv")
    finally:
        conn.close()
