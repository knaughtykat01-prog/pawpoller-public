"""REST API endpoints for the Threads (THR) analytics dashboard.

Official Threads Graph API (OAuth long-lived access token + optional target
user_id). Tracks views, likes, reposts, replies, quotes. Post IDs are numeric
media ids.
"""

from __future__ import annotations
import csv
import io
import logging
from typing import Optional

from fastapi import APIRouter, Query, HTTPException
from fastapi.responses import StreamingResponse

from database.db import get_connection
from database import thr_queries
from polling.thr_poller import run_thr_poll_cycle, thr_poll_progress
from polling.background import spawn_poll
import config

logger = logging.getLogger(__name__)
thr_router = APIRouter(prefix="/api/thr")


# -- THR Auth -----------------------------------------------------------------

@thr_router.get("/auth/status")
def thr_auth_status():
    settings = config.get_settings()
    has_credentials = bool(settings.get("thr_access_token"))
    has_data = False
    conn = get_connection()
    try:
        count = conn.execute("SELECT COUNT(*) as c FROM thr_submissions").fetchone()["c"]
        has_data = count > 0
    except Exception:
        pass
    finally:
        conn.close()
    return {
        "has_credentials": has_credentials,
        "has_data": has_data,
        "username": settings.get("thr_username", "") or settings.get("thr_user_id", ""),
    }


@thr_router.post("/auth/connect")
async def thr_connect(body: dict):
    """Validate a Threads access token (and optional user_id) and save it."""
    access_token = body.get("access_token", "").strip()
    user_id = str(body.get("user_id", "") or "").strip()

    if not access_token:
        raise HTTPException(400, "Access token is required (long-lived token from a Meta app with threads_basic + threads_manage_insights)")

    from polling.thr_poller import _get_or_create_client
    overlay = {
        **config.get_settings(),
        "thr_access_token": access_token,
        "thr_user_id": user_id,
    }
    client = _get_or_create_client(overlay, access_token, user_id)
    try:
        name = await client.validate_session()
    except Exception as e:
        raise HTTPException(502, f"Failed to validate credentials: {e}")

    if not name:
        raise HTTPException(401, "Auth failed — the access token is invalid or lacks the threads_basic / threads_manage_insights scopes.")

    config.save_settings({
        # the client may have refreshed (rotated) the long-lived token
        "thr_access_token": client.access_token,
        "thr_user_id": client.user_id,
        "thr_username": name,
        "thr_notifications_enabled": True,
    })

    return {"status": "success", "message": f"Connected — tracking {name}"}


@thr_router.post("/auth/disconnect")
def thr_disconnect():
    config.delete_settings_keys(["thr_access_token", "thr_user_id", "thr_username"])
    config.save_settings({"thr_notifications_enabled": False})
    return {"status": "success", "message": "Threads disconnected"}


# -- THR Polling --------------------------------------------------------------

@thr_router.get("/poll/progress")
def get_thr_poll_progress():
    return dict(thr_poll_progress)


@thr_router.post("/poll/trigger")
async def trigger_thr_poll():
    try:
        spawn_poll(run_thr_poll_cycle(), "run_thr_poll_cycle")
        return {"status": "started"}
    # Let an explicit HTTPException through — the ownership guard in
    # spawn_poll raises 409 here, and the blanket handler below would
    # otherwise report it as a 500 'internal error'.
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error in THR poll trigger: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))


@thr_router.post("/poll/full-resync")
async def thr_full_resync():
    try:
        spawn_poll(run_thr_poll_cycle(force_full=True), "run_thr_poll_cycle full-resync")
        return {"status": "started"}
    # Let an explicit HTTPException through — the ownership guard in
    # spawn_poll raises 409 here, and the blanket handler below would
    # otherwise report it as a 500 'internal error'.
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error in THR full resync: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))


# -- THR Data -----------------------------------------------------------------

@thr_router.get("/status")
def get_thr_status():
    conn = get_connection()
    try:
        last_poll = thr_queries.get_thr_last_poll(conn)
        count = conn.execute("SELECT COUNT(*) as c FROM thr_submissions").fetchone()["c"]
        snap_count = conn.execute("SELECT COUNT(*) as c FROM thr_snapshots").fetchone()["c"]
        return {
            "total_submissions": count,
            "total_snapshots": snap_count,
            "last_poll": last_poll,
        }
    except Exception as e:
        logger.error("Error in /api/thr/status: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@thr_router.get("/summary")
def get_thr_summary(account_id: int | None = Query(None)):
    conn = get_connection()
    try:
        summary = thr_queries.get_thr_summary(conn, account_id=account_id)
        summary["growth_rates"] = thr_queries.get_thr_growth_rates(conn)
        return summary
    except Exception as e:
        logger.error("Error in /api/thr/summary: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@thr_router.get("/submissions")
def get_thr_submissions(
    sort_by: str = Query("views", description="Sort field"),
    order: str = Query("desc", description="Sort order"),
    search: str = Query("", description="Search title/keywords"),
    account_id: int | None = Query(None),
):
    conn = get_connection()
    try:
        subs = thr_queries.get_all_thr_submissions(conn, sort_by=sort_by, order=order, account_id=account_id)
        deltas = thr_queries.get_thr_submission_deltas(conn)

        if search:
            search_lower = search.lower()
            subs = [s for s in subs if search_lower in s["title"].lower() or search_lower in (s.get("keywords") or "").lower()]

        for s in subs:
            d = deltas.get(s["submission_id"], {})
            s["views_delta"] = d.get("views_delta", 0)
            s["likes_delta"] = d.get("likes_delta", 0)
            s["reposts_delta"] = d.get("reposts_delta", 0)
            s["replies_delta"] = d.get("replies_delta", 0)
            s["quotes_delta"] = d.get("quotes_delta", 0)

        return {"submissions": subs, "total": len(subs)}
    except Exception as e:
        logger.error("Error in /api/thr/submissions: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@thr_router.get("/submissions/{submission_id:path}")
def get_thr_submission(submission_id: str):
    conn = get_connection()
    try:
        sub = thr_queries.get_thr_submission(conn, submission_id)
        if not sub:
            raise HTTPException(status_code=404, detail="Threads post not found")

        full_id = sub["submission_id"]
        snapshots = thr_queries.get_thr_snapshots(conn, full_id)
        growth_rates = thr_queries.get_thr_submission_growth_rates(conn, full_id)
        try:
            tags = conn.execute(
                "SELECT t.tag_id, t.name, t.color FROM tags t JOIN submission_tags st ON t.tag_id = st.tag_id WHERE st.platform = 'thr' AND st.submission_id = ?",
                (full_id,),
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
        logger.error("Error in /api/thr/submissions/%s: %s", submission_id[:50], e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@thr_router.get("/submissions/{submission_id:path}/snapshots")
def get_thr_submission_snapshots(
    submission_id: str,
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
):
    conn = get_connection()
    try:
        return {"snapshots": thr_queries.get_thr_snapshots(conn, submission_id, start, end)}
    except Exception as e:
        logger.error("Error in /api/thr/submissions/%s/snapshots: %s", submission_id[:50], e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@thr_router.get("/aggregate")
def get_thr_aggregate(
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
    account_id: int | None = Query(None),
):
    conn = get_connection()
    try:
        return {"snapshots": thr_queries.get_thr_aggregate_snapshots(conn, start, end, account_id=account_id)}
    except Exception as e:
        logger.error("Error in /api/thr/aggregate: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@thr_router.get("/comparison")
def get_thr_comparison(
    ids: str = Query(..., description="Comma-separated post ids"),
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
):
    conn = get_connection()
    try:
        raw_ids = [x.strip() for x in ids.split(",") if x.strip()]
        if len(raw_ids) > 10:
            raise HTTPException(400, "Max 10 posts for comparison")

        submission_ids = []
        titles = {}
        for rid in raw_ids:
            sub = thr_queries.get_thr_submission(conn, rid)
            if sub:
                submission_ids.append(sub["submission_id"])
                titles[sub["submission_id"]] = sub["title"]

        data = thr_queries.get_thr_comparison_snapshots(conn, submission_ids, start, end)
        return {"series": data, "titles": titles}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error in /api/thr/comparison: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@thr_router.get("/poll_log")
def get_thr_poll_log(limit: int = Query(50, ge=1, le=200)):
    conn = get_connection()
    try:
        return {"polls": thr_queries.get_thr_poll_log(conn, limit)}
    except Exception as e:
        logger.error("Error in /api/thr/poll_log: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


# -- THR CSV Export -----------------------------------------------------------

def _sanitize_csv_value(val):
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


@thr_router.get("/export/submissions")
def export_thr_submissions():
    conn = get_connection()
    try:
        subs = thr_queries.get_all_thr_submissions(conn)
        return _csv_response(subs, "thr_submissions.csv")
    finally:
        conn.close()


@thr_router.get("/export/snapshots")
def export_thr_snapshots(id: str | None = Query(None)):
    conn = get_connection()
    try:
        if id:
            snaps = thr_queries.get_thr_snapshots(conn, id)
        else:
            snaps = [dict(r) for r in conn.execute("SELECT * FROM thr_snapshots ORDER BY polled_at ASC").fetchall()]
        return _csv_response(snaps, f"thr_snapshots{'_' + id[:20] if id else ''}.csv")
    finally:
        conn.close()
