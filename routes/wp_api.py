"""REST API endpoints for the Wattpad (WP) analytics dashboard.

Wattpad provides a public JSON API at api.wattpad.com. No authentication
is required — only a target username is needed. Simpler auth flow than
other platforms: connect just validates the username exists, disconnect
clears it.

Tracks reads, votes, comments, and reading lists (num_lists).
"""

from __future__ import annotations
import csv
import io
import logging
from typing import Optional

from fastapi import APIRouter, Query, HTTPException
from fastapi.responses import StreamingResponse

from database.db import get_connection
from database import wp_queries
from polling.wp_poller import run_wp_poll_cycle, wp_poll_progress
from polling.background import spawn_poll
from clients.wp.client import WPClient
import config

logger = logging.getLogger(__name__)
wp_router = APIRouter(prefix="/api/wp")


# -- WP Auth ---------------------------------------------------------------

@wp_router.get("/auth/status")
def wp_auth_status():
    """Check whether a Wattpad target user is configured and whether there is any WP data."""
    settings = config.get_settings()
    has_credentials = bool(settings.get("wp_target_user"))
    has_data = False
    conn = get_connection()
    try:
        count = conn.execute("SELECT COUNT(*) as c FROM wp_submissions").fetchone()["c"]
        has_data = count > 0
    except Exception:
        pass
    finally:
        conn.close()
    return {
        "has_credentials": has_credentials,
        "has_data": has_data,
        "username": settings.get("wp_target_user", ""),
    }


@wp_router.post("/auth/connect")
async def wp_connect(body: dict):
    """Validate a Wattpad username by checking it exists on the public API.

    Auth flow:
      1. Receive target username from the frontend
      2. Create a temporary WPClient and check the user exists
      3. If valid, save wp_target_user to settings.json
    """
    target_user = body.get("target_user", "").strip()

    if not target_user:
        raise HTTPException(400, "Target user is required (the Wattpad user to track)")

    # Validate against the persistent singleton so a successful
    # check leaves a live session in place for the next poll cycle.
    from polling.wp_poller import _get_or_create_client
    overlay = {**config.get_settings(), "wp_target_user": target_user}
    client = _get_or_create_client(overlay, target_user)
    try:
        result = await client.validate_user()
    except Exception as e:
        raise HTTPException(502, f"Failed to validate user: {e}")

    if not result:
        raise HTTPException(404, "Wattpad user not found — check the username.")

    config.save_settings({
        "wp_target_user": target_user,
        "wp_notifications_enabled": True,
    })

    return {"status": "success", "message": f"Connected — tracking {target_user}"}


@wp_router.post("/auth/disconnect")
def wp_disconnect():
    """Clear Wattpad target user from settings."""
    config.delete_settings_keys(["wp_target_user"])
    config.save_settings({"wp_notifications_enabled": False})
    return {"status": "success", "message": "Wattpad disconnected"}


# -- WP Polling ------------------------------------------------------------

@wp_router.get("/poll/progress")
def get_wp_poll_progress():
    return dict(wp_poll_progress)


@wp_router.post("/poll/trigger")
async def trigger_wp_poll():
    """Manual poll trigger for Wattpad."""
    try:
        spawn_poll(run_wp_poll_cycle(), "run_wp_poll_cycle")
        return {"status": "started"}
    # Let an explicit HTTPException through — the ownership guard in
    # spawn_poll raises 409 here, and the blanket handler below would
    # otherwise report it as a 500 'internal error'.
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error in WP poll trigger: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))


@wp_router.post("/poll/full-resync")
async def wp_full_resync():
    """Force full Wattpad resync."""
    try:
        spawn_poll(run_wp_poll_cycle(force_full=True), "run_wp_poll_cycle full-resync")
        return {"status": "started"}
    # Let an explicit HTTPException through — the ownership guard in
    # spawn_poll raises 409 here, and the blanket handler below would
    # otherwise report it as a 500 'internal error'.
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error in WP full resync: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))


# -- WP Data ---------------------------------------------------------------

@wp_router.get("/status")
def get_wp_status():
    conn = get_connection()
    try:
        last_poll = wp_queries.get_wp_last_poll(conn)
        count = conn.execute("SELECT COUNT(*) as c FROM wp_submissions").fetchone()["c"]
        snap_count = conn.execute("SELECT COUNT(*) as c FROM wp_snapshots").fetchone()["c"]
        return {
            "total_submissions": count,
            "total_snapshots": snap_count,
            "last_poll": last_poll,
        }
    except Exception as e:
        logger.error("Error in /api/wp/status: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@wp_router.get("/summary")
def get_wp_summary(account_id: int | None = Query(None)):
    conn = get_connection()
    try:
        summary = wp_queries.get_wp_summary(conn, account_id=account_id)
        summary["growth_rates"] = wp_queries.get_wp_growth_rates(conn)
        return summary
    except Exception as e:
        logger.error("Error in /api/wp/summary: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@wp_router.get("/submissions")
def get_wp_submissions(
    sort_by: str = Query("reads", description="Sort field"),
    order: str = Query("desc", description="Sort order"),
    search: str = Query("", description="Search title/keywords"),
    rating: str = Query("", description="Filter by rating"),
    account_id: int | None = Query(None),
):
    conn = get_connection()
    try:
        subs = wp_queries.get_all_wp_submissions(conn, sort_by=sort_by, order=order, account_id=account_id)
        deltas = wp_queries.get_wp_submission_deltas(conn)

        if search:
            search_lower = search.lower()
            subs = [s for s in subs if search_lower in s["title"].lower() or search_lower in (s.get("keywords") or "").lower()]
        if rating:
            subs = [s for s in subs if (s.get("rating") or "").lower() == rating.lower()]

        for s in subs:
            d = deltas.get(str(s["submission_id"]), {})
            s["reads_delta"] = d.get("reads_delta", 0)
            s["votes_delta"] = d.get("votes_delta", 0)
            s["comments_delta"] = d.get("comments_delta", 0)
            s["lists_delta"] = d.get("lists_delta", 0)

        return {"submissions": subs, "total": len(subs)}
    except Exception as e:
        logger.error("Error in /api/wp/submissions: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@wp_router.get("/submissions/{submission_id}")
def get_wp_submission(submission_id: int):
    conn = get_connection()
    try:
        sub = wp_queries.get_wp_submission(conn, submission_id)
        if not sub:
            raise HTTPException(status_code=404, detail="Wattpad story not found")
        snapshots = wp_queries.get_wp_snapshots(conn, submission_id)
        growth_rates = wp_queries.get_wp_submission_growth_rates(conn, submission_id)
        try:
            tags = conn.execute(
                "SELECT t.tag_id, t.name, t.color FROM tags t JOIN submission_tags st ON t.tag_id = st.tag_id WHERE st.platform = 'wp' AND st.submission_id = ?",
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
        logger.error("Error in /api/wp/submissions/%s: %s", submission_id, e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@wp_router.get("/submissions/{submission_id}/snapshots")
def get_wp_submission_snapshots(
    submission_id: int,
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
):
    conn = get_connection()
    try:
        return {"snapshots": wp_queries.get_wp_snapshots(conn, submission_id, start, end)}
    except Exception as e:
        logger.error("Error in /api/wp/submissions/%s/snapshots: %s", submission_id, e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@wp_router.get("/aggregate")
def get_wp_aggregate(
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
    account_id: int | None = Query(None),
):
    conn = get_connection()
    try:
        return {"snapshots": wp_queries.get_wp_aggregate_snapshots(conn, start, end, account_id=account_id)}
    except Exception as e:
        logger.error("Error in /api/wp/aggregate: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@wp_router.get("/comparison")
def get_wp_comparison(
    ids: str = Query(..., description="Comma-separated story IDs"),
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
):
    submission_ids = [int(x.strip()) for x in ids.split(",") if x.strip().isdigit()]
    if len(submission_ids) > 10:
        raise HTTPException(400, "Max 10 stories for comparison")

    conn = get_connection()
    try:
        data = wp_queries.get_wp_comparison_snapshots(conn, submission_ids, start, end)
        titles = {}
        for sid in submission_ids:
            sub = wp_queries.get_wp_submission(conn, sid)
            if sub:
                titles[str(sid)] = sub["title"]
        return {"series": data, "titles": titles}
    except Exception as e:
        logger.error("Error in /api/wp/comparison: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@wp_router.get("/poll_log")
def get_wp_poll_log(limit: int = Query(50, ge=1, le=200)):
    conn = get_connection()
    try:
        return {"polls": wp_queries.get_wp_poll_log(conn, limit)}
    except Exception as e:
        logger.error("Error in /api/wp/poll_log: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


# -- WP CSV Export ---------------------------------------------------------

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


@wp_router.get("/export/submissions")
def export_wp_submissions():
    conn = get_connection()
    try:
        subs = wp_queries.get_all_wp_submissions(conn)
        return _csv_response(subs, "wp_submissions.csv")
    finally:
        conn.close()


@wp_router.get("/export/snapshots")
def export_wp_snapshots(id: int | None = Query(None)):
    conn = get_connection()
    try:
        if id:
            snaps = wp_queries.get_wp_snapshots(conn, id)
        else:
            snaps = [dict(r) for r in conn.execute("SELECT * FROM wp_snapshots ORDER BY polled_at ASC").fetchall()]
        return _csv_response(snaps, f"wp_snapshots{'_' + str(id) if id else ''}.csv")
    finally:
        conn.close()
