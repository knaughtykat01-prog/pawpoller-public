"""REST API endpoints for the AO3 (Archive of Our Own) analytics dashboard.

AO3 runs OTW Archive software (same as SquidgeWorld). Auth uses
username/password login with a separate target_user for tracking.
Tracks hits, kudos, comments, bookmarks — plus individual kudos users.
"""

from __future__ import annotations
import csv
import io
import logging
from typing import Optional

from fastapi import APIRouter, Query, HTTPException
from fastapi.responses import StreamingResponse

import config

from database.db import get_connection
from database import ao3_queries
from polling.ao3_poller import run_ao3_poll_cycle, ao3_poll_progress
from polling.background import spawn_poll
from clients.ao3.client import AO3Client
import config

logger = logging.getLogger(__name__)
ao3_router = APIRouter(prefix="/api/ao3")


# -- AO3 Auth ----------------------------------------------------------

@ao3_router.get("/auth/status")
def ao3_auth_status():
    """Check whether AO3 credentials exist and whether there is any AO3 data."""
    settings = config.get_settings()
    has_password = bool(settings.get("ao3_username")) and bool(settings.get("ao3_password"))
    has_cookie = bool(settings.get("ao3_session_cookie"))
    has_credentials = has_password or has_cookie
    has_data = False
    conn = get_connection()
    try:
        count = conn.execute("SELECT COUNT(*) as c FROM ao3_submissions").fetchone()["c"]
        has_data = count > 0
    except Exception:
        pass
    finally:
        conn.close()
    return {
        "has_credentials": has_credentials,
        "has_password": has_password,
        "has_cookie": has_cookie,
        "has_data": has_data,
        "username": settings.get("ao3_target_user", ""),
    }


@ao3_router.post("/auth/connect")
async def ao3_connect(body: dict):
    """Validate AO3 credentials by attempting login.

    Two auth modes are supported:
      1. Password: receive username + password + target_user; do a Rails
         form login and persist the password.
      2. Cookie: receive `session_cookie` (`_otwarchive_session` from the
         user's browser) + target_user; skip the rate-limited login form
         and validate the cookie against /users/{target_user}. This
         bypasses AO3's per-IP login throttle which routinely locks out
         datacenter IPs for 5–60 minutes after a single failed probe.
    """
    username = body.get("username", "").strip()
    password = body.get("password", "").strip()
    target_user = body.get("target_user", "").strip()
    session_cookie = body.get("session_cookie", "").strip()

    if not target_user:
        raise HTTPException(400, "Target user is required (the AO3 user to track)")

    cookie_mode = bool(session_cookie)
    if not cookie_mode and (not username or not password):
        raise HTTPException(400, "Provide either a session cookie or username + password")

    settings = config.get_settings()
    from polling.ao3_poller import _get_or_create_client
    overlay = {
        **settings,
        "ao3_username": username or settings.get("ao3_username", ""),
        "ao3_password": password or settings.get("ao3_password", ""),
        "ao3_target_user": target_user,
        "ao3_session_cookie": session_cookie,
    }
    client = _get_or_create_client(
        overlay, overlay["ao3_username"], overlay["ao3_password"],
        target_user, session_cookie)
    try:
        result = await client.validate_session()
    except Exception as e:
        raise HTTPException(502, f"Failed to validate credentials: {e}")

    if not result:
        if cookie_mode:
            raise HTTPException(401, "Cookie validation failed — copy a fresh `_otwarchive_session` value from your browser (it must be the URL-decoded value, often beginning with a long base64-like string).")
        raise HTTPException(401, "Login failed — check your username and password.")

    saved = {
        "ao3_target_user": target_user,
        "ao3_notifications_enabled": True,
    }
    if cookie_mode:
        saved["ao3_session_cookie"] = session_cookie
        if username:
            saved["ao3_username"] = username
    else:
        saved["ao3_username"] = username
        saved["ao3_password"] = password
    config.save_settings(saved)

    return {"status": "success", "message": f"Connected — tracking {target_user}"}


@ao3_router.post("/auth/disconnect")
def ao3_disconnect():
    """Clear AO3 credentials from settings."""
    config.delete_settings_keys([
        "ao3_username", "ao3_password", "ao3_target_user", "ao3_session_cookie",
    ])
    config.save_settings({"ao3_notifications_enabled": False})
    return {"status": "success", "message": "AO3 disconnected"}


# -- AO3 Polling -------------------------------------------------------

@ao3_router.get("/poll/progress")
def get_ao3_poll_progress():
    return dict(ao3_poll_progress)


@ao3_router.post("/poll/trigger")
async def trigger_ao3_poll():
    """Manual poll trigger for AO3."""
    try:
        spawn_poll(run_ao3_poll_cycle(), "run_ao3_poll_cycle")
        return {"status": "started"}
    # Let an explicit HTTPException through — the ownership guard in
    # spawn_poll raises 409 here, and the blanket handler below would
    # otherwise report it as a 500 'internal error'.
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error in AO3 poll trigger: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))


@ao3_router.post("/poll/full-resync")
async def ao3_full_resync():
    """Force full AO3 resync."""
    try:
        spawn_poll(run_ao3_poll_cycle(force_full=True), "run_ao3_poll_cycle full-resync")
        return {"status": "started"}
    # Let an explicit HTTPException through — the ownership guard in
    # spawn_poll raises 409 here, and the blanket handler below would
    # otherwise report it as a 500 'internal error'.
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error in AO3 full resync: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))


# -- AO3 Data ----------------------------------------------------------

@ao3_router.get("/status")
def get_ao3_status():
    conn = get_connection()
    try:
        last_poll = ao3_queries.get_ao3_last_poll(conn)
        count = conn.execute("SELECT COUNT(*) as c FROM ao3_submissions").fetchone()["c"]
        snap_count = conn.execute("SELECT COUNT(*) as c FROM ao3_snapshots").fetchone()["c"]
        return {
            "total_submissions": count,
            "total_snapshots": snap_count,
            "last_poll": last_poll,
        }
    except Exception as e:
        logger.error("Error in /api/ao3/status: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@ao3_router.get("/summary")
def get_ao3_summary(account_id: int | None = Query(None)):
    conn = get_connection()
    try:
        summary = ao3_queries.get_ao3_summary(conn, account_id=account_id)
        summary["growth_rates"] = ao3_queries.get_ao3_growth_rates(conn)
        return summary
    except Exception as e:
        logger.error("Error in /api/ao3/summary: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@ao3_router.get("/submissions")
def get_ao3_submissions(
    sort_by: str = Query("views", description="Sort field"),
    order: str = Query("desc", description="Sort order"),
    search: str = Query("", description="Search title/keywords"),
    rating: str = Query("", description="Filter by rating"),
    account_id: int | None = Query(None),
):
    conn = get_connection()
    try:
        subs = ao3_queries.get_all_ao3_submissions(conn, sort_by=sort_by, order=order, account_id=account_id)
        deltas = ao3_queries.get_ao3_submission_deltas(conn)

        if search:
            search_lower = search.lower()
            subs = [s for s in subs if search_lower in s["title"].lower() or search_lower in (s.get("keywords") or "").lower()]
        if rating:
            subs = [s for s in subs if (s.get("rating") or "").lower() == rating.lower()]

        for s in subs:
            d = deltas.get(str(s["submission_id"]), {})
            s["views_delta"] = d.get("views_delta", 0)
            s["faves_delta"] = d.get("faves_delta", 0)
            s["comments_delta"] = d.get("comments_delta", 0)
            s["bookmarks_delta"] = d.get("bookmarks_delta", 0)

        return {"submissions": subs, "total": len(subs)}
    except Exception as e:
        logger.error("Error in /api/ao3/submissions: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@ao3_router.get("/submissions/{submission_id}")
def get_ao3_submission(submission_id: int):
    conn = get_connection()
    try:
        sub = ao3_queries.get_ao3_submission(conn, submission_id)
        if not sub:
            raise HTTPException(status_code=404, detail="AO3 work not found")
        snapshots = ao3_queries.get_ao3_snapshots(conn, submission_id)
        growth_rates = ao3_queries.get_ao3_submission_growth_rates(conn, submission_id)
        kudos_users = ao3_queries.get_ao3_kudos_users(conn, submission_id)
        try:
            tags = conn.execute(
                "SELECT t.tag_id, t.name, t.color FROM tags t JOIN submission_tags st ON t.tag_id = st.tag_id WHERE st.platform = 'ao3' AND st.submission_id = ?",
                (submission_id,),
            ).fetchall()
        except Exception:
            tags = []
        sub_dict = dict(sub) if not isinstance(sub, dict) else sub
        sub_dict["tags"] = [dict(r) for r in tags]
        return {
            "submission": sub_dict,
            "snapshots": snapshots,
            "growth_rates": growth_rates,
            "kudos_users": kudos_users,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error in /api/ao3/submissions/%s: %s", submission_id, e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@ao3_router.get("/submissions/{submission_id}/snapshots")
def get_ao3_submission_snapshots(
    submission_id: int,
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
):
    conn = get_connection()
    try:
        return {"snapshots": ao3_queries.get_ao3_snapshots(conn, submission_id, start, end)}
    except Exception as e:
        logger.error("Error in /api/ao3/submissions/%s/snapshots: %s", submission_id, e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@ao3_router.get("/aggregate")
def get_ao3_aggregate(
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
    account_id: int | None = Query(None),
):
    conn = get_connection()
    try:
        return {"snapshots": ao3_queries.get_ao3_aggregate_snapshots(conn, start, end, account_id=account_id)}
    except Exception as e:
        logger.error("Error in /api/ao3/aggregate: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@ao3_router.get("/comparison")
def get_ao3_comparison(
    ids: str = Query(..., description="Comma-separated work IDs"),
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
):
    submission_ids = [int(x.strip()) for x in ids.split(",") if x.strip().isdigit()]
    if len(submission_ids) > 10:
        raise HTTPException(400, "Max 10 works for comparison")

    conn = get_connection()
    try:
        data = ao3_queries.get_ao3_comparison_snapshots(conn, submission_ids, start, end)
        titles = {}
        for sid in submission_ids:
            sub = ao3_queries.get_ao3_submission(conn, sid)
            if sub:
                titles[str(sid)] = sub["title"]
        return {"series": data, "titles": titles}
    except Exception as e:
        logger.error("Error in /api/ao3/comparison: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@ao3_router.get("/poll_log")
def get_ao3_poll_log(limit: int = Query(50, ge=1, le=200)):
    conn = get_connection()
    try:
        return {"polls": ao3_queries.get_ao3_poll_log(conn, limit)}
    except Exception as e:
        logger.error("Error in /api/ao3/poll_log: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


# -- AO3 CSV Export ----------------------------------------------------

def _sanitize_csv_value(val):
    """Prevent CSV formula injection — prefix dangerous chars with single quote."""
    if isinstance(val, str) and val and val[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + val
    return val


def _csv_response(rows: list[dict], filename: str) -> StreamingResponse:
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


@ao3_router.get("/export/submissions")
def export_ao3_submissions():
    conn = get_connection()
    try:
        subs = ao3_queries.get_all_ao3_submissions(conn)
        return _csv_response(subs, "ao3_submissions.csv")
    finally:
        conn.close()


@ao3_router.get("/export/snapshots")
def export_ao3_snapshots(id: int | None = Query(None)):
    conn = get_connection()
    try:
        if id:
            snaps = ao3_queries.get_ao3_snapshots(conn, id)
        else:
            snaps = [dict(r) for r in conn.execute("SELECT * FROM ao3_snapshots ORDER BY polled_at ASC").fetchall()]
        return _csv_response(snaps, f"ao3_snapshots{'_' + str(id) if id else ''}.csv")
    finally:
        conn.close()
