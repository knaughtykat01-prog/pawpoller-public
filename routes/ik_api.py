"""REST API endpoints for the Itaku (IK) analytics dashboard.

Itaku provides a public REST API at itaku.ee/api/. No authentication
is required — only a target username is needed. Simpler auth flow than
other platforms: connect just validates the username exists, disconnect
clears it.

Tracks likes, comments, and reshares. NO views metric available.
Content types: images and posts.
"""

from __future__ import annotations
import csv
import io
import logging
from typing import Optional

from fastapi import APIRouter, Query, HTTPException
from fastapi.responses import StreamingResponse

from database.db import get_connection
from database import ik_queries
from polling.ik_poller import run_ik_poll_cycle, ik_poll_progress
from polling.background import spawn_poll
from clients.ik.client import IKClient
import config

logger = logging.getLogger(__name__)
ik_router = APIRouter(prefix="/api/ik")


# -- IK Auth ---------------------------------------------------------------

@ik_router.get("/auth/status")
def ik_auth_status():
    """Check whether an Itaku target user is configured and whether there is any IK data."""
    settings = config.get_settings()
    has_credentials = bool(settings.get("ik_target_user"))
    has_data = False
    conn = get_connection()
    try:
        count = conn.execute("SELECT COUNT(*) as c FROM ik_submissions").fetchone()["c"]
        has_data = count > 0
    except Exception:
        pass
    finally:
        conn.close()
    return {
        "has_credentials": has_credentials,
        "has_data": has_data,
        "username": settings.get("ik_target_user", ""),
        # Tracking needs only the username; POSTING needs the auth token. Surface
        # it so the UI can show posting-readiness + an add-token affordance.
        "has_auth_token": bool(settings.get("ik_auth_token")),
    }


@ik_router.post("/auth/connect")
async def ik_connect(body: dict):
    """Validate an Itaku username by checking it exists on the public API.

    Auth flow:
      1. Receive target username (+ optional auth token) from the frontend
      2. Create a temporary IKClient and check the user exists
      3. If valid, save ik_target_user (+ ik_auth_token if given) to settings

    The username alone enables tracking/reading; the optional auth token is what
    POSTING to Itaku needs (it authenticates the upload as your account).
    """
    target_user = body.get("target_user", "").strip()
    auth_token = (body.get("auth_token") or "").strip()

    if not target_user:
        raise HTTPException(400, "Target user is required (the Itaku user to track)")

    # Validate against the persistent singleton so a successful
    # check leaves a live session in place for the next poll cycle.
    from polling.ik_poller import _get_or_create_client
    overlay = {**config.get_settings(), "ik_target_user": target_user}
    client = _get_or_create_client(overlay, target_user)
    try:
        result = await client.validate_user()
    except Exception as e:
        raise HTTPException(502, f"Failed to validate user: {e}")

    if not result:
        raise HTTPException(404, "Itaku user not found — check the username.")

    payload = {"ik_target_user": target_user, "ik_notifications_enabled": True}
    if auth_token:
        payload["ik_auth_token"] = auth_token   # CREDENTIAL_FIELD → auto-vaulted
    config.save_settings(payload)

    msg = f"Connected — tracking {target_user}"
    if auth_token:
        msg += " (auth token saved — posting enabled)"
    return {"status": "success", "message": msg}


@ik_router.post("/auth/token")
async def ik_set_token(body: dict):
    """Set / replace / clear the Itaku auth token WITHOUT re-validating the
    username — for an already-connected account that wants to enable posting.
    Tracking works without it; posting needs it. An empty token clears it.

    **The token is checked against Itaku before it is saved.** It used to be
    stored unverified, and because tracking does not need it a wrong value sat
    there looking connected until the next upload failed — which is exactly how
    2026-08-19's `Showing_Off` post died with a 401. A token that does not
    authenticate is refused here instead, where the person who pasted it is
    still looking at the box they pasted it into.
    """
    token = (body.get("auth_token") or "").strip()
    if not token:
        config.delete_settings_keys(["ik_auth_token"])
        return {"status": "cleared", "message": "Auth token cleared — tracking only"}

    from clients.ik.client import IKClient
    settings = config.get_settings()
    client = IKClient(settings.get("ik_target_user", ""))
    try:
        result = await client.validate_token(token)
    finally:
        await client.close()

    if result["status"] == "malformed":
        raise HTTPException(400, "Itaku rejected the header, not the value — the token has a "
                                 "space in it. Paste only the part after 'Token '.")
    if result["status"] == "invalid":
        raise HTTPException(401, "Itaku did not accept that token. It is not the `sessionid` "
                                 "cookie — open DevTools → Network → Fetch/XHR, click any "
                                 "request to itaku.ee/api/, and copy the Authorization header's "
                                 "value after 'Token '.")
    if result["status"] == "error":
        raise HTTPException(502, result["detail"])

    config.save_settings({"ik_auth_token": token})
    who = result.get("username")
    return {"status": "success",
            "message": f"Auth token saved — posting enabled{f' as {who}' if who else ''}"}


@ik_router.post("/auth/disconnect")
def ik_disconnect():
    """Clear Itaku target user + auth token from settings."""
    config.delete_settings_keys(["ik_target_user", "ik_auth_token"])
    config.save_settings({"ik_notifications_enabled": False})
    return {"status": "success", "message": "Itaku disconnected"}


# -- IK Polling ------------------------------------------------------------

@ik_router.get("/poll/progress")
def get_ik_poll_progress():
    return dict(ik_poll_progress)


@ik_router.post("/poll/trigger")
async def trigger_ik_poll():
    """Manual poll trigger for Itaku."""
    try:
        spawn_poll(run_ik_poll_cycle(), "run_ik_poll_cycle")
        return {"status": "started"}
    # Let an explicit HTTPException through — the ownership guard in
    # spawn_poll raises 409 here, and the blanket handler below would
    # otherwise report it as a 500 'internal error'.
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error in IK poll trigger: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))


@ik_router.post("/poll/full-resync")
async def ik_full_resync():
    """Force full Itaku resync."""
    try:
        spawn_poll(run_ik_poll_cycle(force_full=True), "run_ik_poll_cycle full-resync")
        return {"status": "started"}
    # Let an explicit HTTPException through — the ownership guard in
    # spawn_poll raises 409 here, and the blanket handler below would
    # otherwise report it as a 500 'internal error'.
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error in IK full resync: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))


# -- IK Data ---------------------------------------------------------------

@ik_router.get("/status")
def get_ik_status():
    conn = get_connection()
    try:
        last_poll = ik_queries.get_ik_last_poll(conn)
        count = conn.execute("SELECT COUNT(*) as c FROM ik_submissions").fetchone()["c"]
        snap_count = conn.execute("SELECT COUNT(*) as c FROM ik_snapshots").fetchone()["c"]
        return {
            "total_submissions": count,
            "total_snapshots": snap_count,
            "last_poll": last_poll,
        }
    except Exception as e:
        logger.error("Error in /api/ik/status: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@ik_router.get("/summary")
def get_ik_summary(account_id: int | None = Query(None)):
    conn = get_connection()
    try:
        summary = ik_queries.get_ik_summary(conn, account_id=account_id)
        summary["growth_rates"] = ik_queries.get_ik_growth_rates(conn)
        return summary
    except Exception as e:
        logger.error("Error in /api/ik/summary: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@ik_router.get("/submissions")
def get_ik_submissions(
    sort_by: str = Query("likes", description="Sort field"),
    order: str = Query("desc", description="Sort order"),
    search: str = Query("", description="Search title/keywords"),
    rating: str = Query("", description="Filter by rating"),
    content_type: str = Query("", description="Filter by content type (image/post)"),
    account_id: int | None = Query(None),
):
    conn = get_connection()
    try:
        subs = ik_queries.get_all_ik_submissions(conn, sort_by=sort_by, order=order, account_id=account_id)
        deltas = ik_queries.get_ik_submission_deltas(conn)

        if search:
            search_lower = search.lower()
            subs = [s for s in subs if search_lower in s["title"].lower() or search_lower in (s.get("keywords") or "").lower()]
        if rating:
            subs = [s for s in subs if (s.get("rating") or "").lower() == rating.lower()]
        if content_type:
            subs = [s for s in subs if (s.get("content_type") or "").lower() == content_type.lower()]

        for s in subs:
            d = deltas.get(str(s["submission_id"]), {})
            s["likes_delta"] = d.get("likes_delta", 0)
            s["comments_delta"] = d.get("comments_delta", 0)
            s["reshares_delta"] = d.get("reshares_delta", 0)

        return {"submissions": subs, "total": len(subs)}
    except Exception as e:
        logger.error("Error in /api/ik/submissions: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@ik_router.get("/submissions/{submission_id}")
def get_ik_submission(submission_id: int):
    conn = get_connection()
    try:
        sub = ik_queries.get_ik_submission(conn, submission_id)
        if not sub:
            raise HTTPException(status_code=404, detail="Itaku content not found")
        snapshots = ik_queries.get_ik_snapshots(conn, submission_id)
        growth_rates = ik_queries.get_ik_submission_growth_rates(conn, submission_id)
        try:
            tags = conn.execute(
                "SELECT t.tag_id, t.name, t.color FROM tags t JOIN submission_tags st ON t.tag_id = st.tag_id WHERE st.platform = 'ik' AND st.submission_id = ?",
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
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error in /api/ik/submissions/%s: %s", submission_id, e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@ik_router.get("/submissions/{submission_id}/snapshots")
def get_ik_submission_snapshots(
    submission_id: int,
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
):
    conn = get_connection()
    try:
        return {"snapshots": ik_queries.get_ik_snapshots(conn, submission_id, start, end)}
    except Exception as e:
        logger.error("Error in /api/ik/submissions/%s/snapshots: %s", submission_id, e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@ik_router.get("/aggregate")
def get_ik_aggregate(
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
    account_id: int | None = Query(None),
):
    conn = get_connection()
    try:
        return {"snapshots": ik_queries.get_ik_aggregate_snapshots(conn, start, end, account_id=account_id)}
    except Exception as e:
        logger.error("Error in /api/ik/aggregate: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@ik_router.get("/comparison")
def get_ik_comparison(
    ids: str = Query(..., description="Comma-separated content IDs"),
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
):
    submission_ids = [int(x.strip()) for x in ids.split(",") if x.strip().isdigit()]
    if len(submission_ids) > 10:
        raise HTTPException(400, "Max 10 items for comparison")

    conn = get_connection()
    try:
        data = ik_queries.get_ik_comparison_snapshots(conn, submission_ids, start, end)
        titles = {}
        for sid in submission_ids:
            sub = ik_queries.get_ik_submission(conn, sid)
            if sub:
                titles[str(sid)] = sub["title"]
        return {"series": data, "titles": titles}
    except Exception as e:
        logger.error("Error in /api/ik/comparison: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@ik_router.get("/poll_log")
def get_ik_poll_log(limit: int = Query(50, ge=1, le=200)):
    conn = get_connection()
    try:
        return {"polls": ik_queries.get_ik_poll_log(conn, limit)}
    except Exception as e:
        logger.error("Error in /api/ik/poll_log: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


# -- IK CSV Export ---------------------------------------------------------

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


@ik_router.get("/export/submissions")
def export_ik_submissions():
    conn = get_connection()
    try:
        subs = ik_queries.get_all_ik_submissions(conn)
        return _csv_response(subs, "ik_submissions.csv")
    finally:
        conn.close()


@ik_router.get("/export/snapshots")
def export_ik_snapshots(id: int | None = Query(None)):
    conn = get_connection()
    try:
        if id:
            snaps = ik_queries.get_ik_snapshots(conn, id)
        else:
            snaps = [dict(r) for r in conn.execute("SELECT * FROM ik_snapshots ORDER BY polled_at ASC").fetchall()]
        return _csv_response(snaps, f"ik_snapshots{'_' + str(id) if id else ''}.csv")
    finally:
        conn.close()
