"""REST API endpoints for the SquidgeWorld analytics dashboard.

SquidgeWorld runs OTW Archive software (same as AO3). Auth uses
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

from database.db import get_connection
from database import sqw_queries
from polling.sqw_poller import run_sqw_poll_cycle, sqw_poll_progress
from polling.background import spawn_poll
from clients.sqw.client import SquidgeWorldClient
import config

logger = logging.getLogger(__name__)
sqw_router = APIRouter(prefix="/api/sqw")


# -- SqW Auth ----------------------------------------------------------

@sqw_router.get("/auth/status")
def sqw_auth_status():
    """Check whether SquidgeWorld credentials exist and whether there is any SqW data."""
    settings = config.get_settings()
    has_credentials = bool(settings.get("sqw_username")) and bool(settings.get("sqw_password"))
    has_data = False
    conn = get_connection()
    try:
        count = conn.execute("SELECT COUNT(*) as c FROM sqw_submissions").fetchone()["c"]
        has_data = count > 0
    except Exception:
        pass
    finally:
        conn.close()
    return {
        "has_credentials": has_credentials,
        "has_data": has_data,
        "username": settings.get("sqw_target_user", ""),
    }


@sqw_router.post("/auth/connect")
async def sqw_connect(body: dict):
    """Validate SquidgeWorld credentials by attempting login.

    Auth flow:
      1. Receive login username + password + target user from the frontend
      2. Create a temporary SquidgeWorldClient and attempt login
      3. If login succeeds, save credentials to settings.json
    """
    username = body.get("username", "").strip()
    password = body.get("password", "").strip()
    target_user = body.get("target_user", "").strip()

    if not username or not password:
        raise HTTPException(400, "Username and password are required")
    if not target_user:
        raise HTTPException(400, "Target user is required (the SquidgeWorld user to track)")

    # Use the persistent singleton so a successful login leaves a live
    # session (with cached Anubis token + cookies) in place for imports
    # and the next poll cycle to reuse.
    from polling.sqw_poller import _get_or_create_client
    overlay = {
        **config.get_settings(),
        "sqw_username": username,
        "sqw_password": password,
        "sqw_target_user": target_user,
    }
    client = _get_or_create_client(overlay, username, password, target_user)
    try:
        result = await client.validate_session()
    except Exception as e:
        raise HTTPException(502, f"Failed to validate credentials: {e}")

    if not result:
        raise HTTPException(401, "Login failed — check your username and password.")

    config.save_settings({
        "sqw_username": username,
        "sqw_password": password,
        "sqw_target_user": target_user,
        "sqw_notifications_enabled": True,
    })

    return {"status": "success", "message": f"Connected — tracking {target_user}"}


@sqw_router.post("/auth/disconnect")
def sqw_disconnect():
    """Clear SquidgeWorld credentials from settings."""
    config.delete_settings_keys(["sqw_username", "sqw_password", "sqw_target_user"])
    config.save_settings({"sqw_notifications_enabled": False})
    return {"status": "success", "message": "SquidgeWorld disconnected"}


# -- SqW Polling -------------------------------------------------------

@sqw_router.get("/poll/progress")
def get_sqw_poll_progress():
    return dict(sqw_poll_progress)


@sqw_router.post("/poll/trigger")
async def trigger_sqw_poll():
    """Manual poll trigger for SquidgeWorld."""
    try:
        spawn_poll(run_sqw_poll_cycle(), "run_sqw_poll_cycle")
        return {"status": "started"}
    # Let an explicit HTTPException through — the ownership guard in
    # spawn_poll raises 409 here, and the blanket handler below would
    # otherwise report it as a 500 'internal error'.
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error in SqW poll trigger: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))


@sqw_router.post("/poll/full-resync")
async def sqw_full_resync():
    """Force full SquidgeWorld resync."""
    try:
        spawn_poll(run_sqw_poll_cycle(force_full=True), "run_sqw_poll_cycle full-resync")
        return {"status": "started"}
    # Let an explicit HTTPException through — the ownership guard in
    # spawn_poll raises 409 here, and the blanket handler below would
    # otherwise report it as a 500 'internal error'.
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error in SqW full resync: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))


# -- SqW Data ----------------------------------------------------------

@sqw_router.get("/status")
def get_sqw_status():
    conn = get_connection()
    try:
        last_poll = sqw_queries.get_sqw_last_poll(conn)
        count = conn.execute("SELECT COUNT(*) as c FROM sqw_submissions").fetchone()["c"]
        snap_count = conn.execute("SELECT COUNT(*) as c FROM sqw_snapshots").fetchone()["c"]
        return {
            "total_submissions": count,
            "total_snapshots": snap_count,
            "last_poll": last_poll,
        }
    except Exception as e:
        logger.error("Error in /api/sqw/status: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@sqw_router.get("/summary")
def get_sqw_summary(account_id: int | None = Query(None)):
    conn = get_connection()
    try:
        summary = sqw_queries.get_sqw_summary(conn, account_id=account_id)
        summary["growth_rates"] = sqw_queries.get_sqw_growth_rates(conn)
        return summary
    except Exception as e:
        logger.error("Error in /api/sqw/summary: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@sqw_router.get("/submissions")
def get_sqw_submissions(
    sort_by: str = Query("views", description="Sort field"),
    order: str = Query("desc", description="Sort order"),
    search: str = Query("", description="Search title/keywords"),
    rating: str = Query("", description="Filter by rating"),
    account_id: int | None = Query(None),
):
    conn = get_connection()
    try:
        subs = sqw_queries.get_all_sqw_submissions(conn, sort_by=sort_by, order=order, account_id=account_id)
        deltas = sqw_queries.get_sqw_submission_deltas(conn)

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
        logger.error("Error in /api/sqw/submissions: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@sqw_router.get("/submissions/{submission_id}")
def get_sqw_submission(submission_id: int):
    conn = get_connection()
    try:
        sub = sqw_queries.get_sqw_submission(conn, submission_id)
        if not sub:
            raise HTTPException(status_code=404, detail="SqW work not found")
        snapshots = sqw_queries.get_sqw_snapshots(conn, submission_id)
        growth_rates = sqw_queries.get_sqw_submission_growth_rates(conn, submission_id)
        kudos_users = sqw_queries.get_sqw_kudos_users(conn, submission_id)
        try:
            tags = conn.execute(
                "SELECT t.tag_id, t.name, t.color FROM tags t JOIN submission_tags st ON t.tag_id = st.tag_id WHERE st.platform = 'sqw' AND st.submission_id = ?",
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
        logger.error("Error in /api/sqw/submissions/%s: %s", submission_id, e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@sqw_router.get("/submissions/{submission_id}/snapshots")
def get_sqw_submission_snapshots(
    submission_id: int,
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
):
    conn = get_connection()
    try:
        return {"snapshots": sqw_queries.get_sqw_snapshots(conn, submission_id, start, end)}
    except Exception as e:
        logger.error("Error in /api/sqw/submissions/%s/snapshots: %s", submission_id, e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@sqw_router.get("/aggregate")
def get_sqw_aggregate(
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
    account_id: int | None = Query(None),
):
    conn = get_connection()
    try:
        return {"snapshots": sqw_queries.get_sqw_aggregate_snapshots(conn, start, end, account_id=account_id)}
    except Exception as e:
        logger.error("Error in /api/sqw/aggregate: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@sqw_router.get("/comparison")
def get_sqw_comparison(
    ids: str = Query(..., description="Comma-separated work IDs"),
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
):
    submission_ids = [int(x.strip()) for x in ids.split(",") if x.strip().isdigit()]
    if len(submission_ids) > 10:
        raise HTTPException(400, "Max 10 works for comparison")

    conn = get_connection()
    try:
        data = sqw_queries.get_sqw_comparison_snapshots(conn, submission_ids, start, end)
        titles = {}
        for sid in submission_ids:
            sub = sqw_queries.get_sqw_submission(conn, sid)
            if sub:
                titles[str(sid)] = sub["title"]
        return {"series": data, "titles": titles}
    except Exception as e:
        logger.error("Error in /api/sqw/comparison: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@sqw_router.get("/poll_log")
def get_sqw_poll_log(limit: int = Query(50, ge=1, le=200)):
    conn = get_connection()
    try:
        return {"polls": sqw_queries.get_sqw_poll_log(conn, limit)}
    except Exception as e:
        logger.error("Error in /api/sqw/poll_log: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


# -- SqW CSV Export ----------------------------------------------------

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


@sqw_router.get("/export/submissions")
def export_sqw_submissions():
    conn = get_connection()
    try:
        subs = sqw_queries.get_all_sqw_submissions(conn)
        return _csv_response(subs, "squidgeworld_submissions.csv")
    finally:
        conn.close()


@sqw_router.get("/export/snapshots")
def export_sqw_snapshots(id: int | None = Query(None)):
    conn = get_connection()
    try:
        if id:
            snaps = sqw_queries.get_sqw_snapshots(conn, id)
        else:
            snaps = [dict(r) for r in conn.execute("SELECT * FROM sqw_snapshots ORDER BY polled_at ASC").fetchall()]
        return _csv_response(snaps, f"squidgeworld_snapshots{'_' + str(id) if id else ''}.csv")
    finally:
        conn.close()
