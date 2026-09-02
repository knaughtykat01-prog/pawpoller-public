"""REST API endpoints for the SoFurry analytics dashboard.

Mirrors the structure of ws_api.py (Weasyl) for frontend consistency.
SoFurry uses email/password authentication (no API key), so the auth
endpoints accept username + password instead of an API key.

Key differences from other platforms:
  - submission_id is TEXT (alphanumeric), not INTEGER
  - Auth uses email/password login, not API key or cookies
  - No comments endpoint (count only, like Weasyl)
  - No faving_users endpoint (count only)
"""

from __future__ import annotations
import csv
import io
import logging
from typing import Optional

from fastapi import APIRouter, Query, HTTPException
from fastapi.responses import StreamingResponse

from database.db import get_connection
from database import sf_queries
from polling.sf_poller import run_sf_poll_cycle, sf_poll_progress
from polling.background import spawn_poll
from clients.sf.client import SoFurryClient, SOFURRY_PAT_URL
import config

logger = logging.getLogger(__name__)
sf_router = APIRouter(prefix="/api/sf")


# -- SF Auth -----------------------------------------------------------

@sf_router.get("/auth/status")
def sf_auth_status():
    """Check whether a SoFurry API token exists and whether there is any SF data."""
    settings = config.get_settings()
    has_credentials = bool(settings.get("sf_api_token"))
    has_data = False
    conn = get_connection()
    try:
        count = conn.execute("SELECT COUNT(*) as c FROM sf_submissions").fetchone()["c"]
        has_data = count > 0
    except Exception:
        pass
    finally:
        conn.close()
    return {
        "has_credentials": has_credentials,
        "has_data": has_data,
        "username": settings.get("sf_display_name", ""),
        "token_url": SOFURRY_PAT_URL,
    }


@sf_router.post("/auth/connect")
async def sf_connect(body: dict):
    """Validate a SoFurry Personal Access Token and save it.

    3.4.0: SoFurry shipped an official API, so this no longer takes an email,
    password or 2FA code — none of which PawPoller stores any more. The user
    creates a token at ``sofurry.com/settings/pat-create`` and pastes it here.

    The handle is not asked for either: ``GET /v1/user/me`` returns the canonical
    one, so it is derived from the token rather than typed (and therefore cannot
    be mistyped, which used to produce a recurring "could not verify display name"
    warning on every poll cycle).
    """
    api_token = (body.get("api_token") or body.get("token") or "").strip()
    if not api_token:
        raise HTTPException(
            400,
            "A SoFurry Personal Access Token is required — create one at "
            f"{SOFURRY_PAT_URL}",
        )

    # Validate against the persistent singleton so the poller reuses this client.
    from polling.sf_poller import _get_or_create_client
    overlay = {**config.get_settings(), "sf_api_token": api_token}
    # is_default=True makes account_id irrelevant to the settings-key lookup,
    # so 0 is a safe placeholder for the default-account connect flow.
    client = _get_or_create_client(overlay, 0, True)
    try:
        display_name = await client.validate_token()
    except Exception as e:
        raise HTTPException(502, f"Failed to validate token: {e}")

    if not display_name:
        raise HTTPException(
            401,
            "SoFurry rejected that token. Check you pasted it whole, and that it "
            "has not been revoked or expired.",
        )

    config.save_settings({
        "sf_api_token": api_token,
        "sf_display_name": display_name,
        "sf_notifications_enabled": True,
    })

    return {"status": "success", "message": f"Connected as {display_name}"}


@sf_router.post("/auth/disconnect")
def sf_disconnect():
    """Clear the SoFurry token from settings.

    Also clears the pre-3.4.0 login fields, so disconnecting scrubs a vault that
    the startup migration has not run against yet.
    """
    config.delete_settings_keys([
        "sf_api_token", "sf_display_name",
        # legacy, pre-3.4.0
        "sf_username", "sf_password", "sf_totp_code", "sf_session_cookies",
    ])
    config.save_settings({"sf_notifications_enabled": False})
    return {"status": "success", "message": "SoFurry disconnected"}


# -- SF Polling --------------------------------------------------------

@sf_router.get("/poll/progress")
def get_sf_poll_progress():
    return dict(sf_poll_progress)


@sf_router.post("/poll/trigger")
async def trigger_sf_poll():
    """Manual poll trigger for SoFurry."""
    try:
        spawn_poll(run_sf_poll_cycle(), "run_sf_poll_cycle")
        return {"status": "started"}
    # Let an explicit HTTPException through — the ownership guard in
    # spawn_poll raises 409 here, and the blanket handler below would
    # otherwise report it as a 500 'internal error'.
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error in SF poll trigger: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))


@sf_router.post("/poll/full-resync")
async def sf_full_resync():
    """Force full SoFurry resync."""
    try:
        spawn_poll(run_sf_poll_cycle(force_full=True), "run_sf_poll_cycle full-resync")
        return {"status": "started"}
    # Let an explicit HTTPException through — the ownership guard in
    # spawn_poll raises 409 here, and the blanket handler below would
    # otherwise report it as a 500 'internal error'.
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error in SF full resync: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))


# -- SF Data -----------------------------------------------------------

@sf_router.get("/status")
def get_sf_status():
    conn = get_connection()
    try:
        last_poll = sf_queries.get_sf_last_poll(conn)
        count = conn.execute("SELECT COUNT(*) as c FROM sf_submissions").fetchone()["c"]
        snap_count = conn.execute("SELECT COUNT(*) as c FROM sf_snapshots").fetchone()["c"]
        return {
            "total_submissions": count,
            "total_snapshots": snap_count,
            "last_poll": last_poll,
        }
    except Exception as e:
        logger.error("Error in /api/sf/status: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@sf_router.get("/summary")
def get_sf_summary(account_id: int | None = Query(None)):
    conn = get_connection()
    try:
        summary = sf_queries.get_sf_summary(conn, account_id=account_id)
        # growth_rates + watcher counts stay aggregate (not account-scoped), mirroring IB.
        summary["growth_rates"] = sf_queries.get_sf_growth_rates(conn)
        summary["total_watchers"] = sf_queries.get_sf_watchers_count(conn)
        summary["recent_watchers"] = sf_queries.get_sf_recent_watchers(conn, limit=10)
        return summary
    except Exception as e:
        logger.error("Error in /api/sf/summary: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@sf_router.get("/watchers")
def get_sf_watchers():
    """Recent SF followers list with total count."""
    conn = get_connection()
    try:
        watchers = sf_queries.get_sf_recent_watchers(conn, limit=50)
        count = sf_queries.get_sf_watchers_count(conn)
        return {"watchers": watchers, "total": count}
    finally:
        conn.close()


@sf_router.get("/submissions")
def get_sf_submissions(
    sort_by: str = Query("views", description="Sort field"),
    order: str = Query("desc", description="Sort order"),
    search: str = Query("", description="Search title/keywords"),
    rating: str = Query("", description="Filter by rating"),
    account_id: int | None = Query(None),
):
    conn = get_connection()
    try:
        subs = sf_queries.get_all_sf_submissions(conn, sort_by=sort_by, order=order, account_id=account_id)
        deltas = sf_queries.get_sf_submission_deltas(conn)

        if search:
            search_lower = search.lower()
            subs = [s for s in subs if search_lower in s["title"].lower() or search_lower in (s.get("keywords") or "").lower()]
        if rating:
            subs = [s for s in subs if (s.get("rating") or "").lower() == rating.lower()]

        for s in subs:
            d = deltas.get(s["submission_id"], {})
            s["views_delta"] = d.get("views_delta", 0)
            s["faves_delta"] = d.get("faves_delta", 0)
            s["comments_delta"] = d.get("comments_delta", 0)

        return {"submissions": subs, "total": len(subs)}
    except Exception as e:
        logger.error("Error in /api/sf/submissions: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@sf_router.get("/submissions/{submission_id}")
def get_sf_submission(submission_id: str):
    conn = get_connection()
    try:
        sub = sf_queries.get_sf_submission(conn, submission_id)
        if not sub:
            raise HTTPException(status_code=404, detail="SF submission not found")
        snapshots = sf_queries.get_sf_snapshots(conn, submission_id)
        growth_rates = sf_queries.get_sf_submission_growth_rates(conn, submission_id)
        try:
            tags = conn.execute(
                "SELECT t.tag_id, t.name, t.color FROM tags t JOIN submission_tags st ON t.tag_id = st.tag_id WHERE st.platform = 'sf' AND st.submission_id = ?",
                (submission_id,),
            ).fetchall()
        except Exception:
            tags = []
        sub_dict = dict(sub) if not isinstance(sub, dict) else sub
        sub_dict["tags"] = [dict(r) for r in tags]
        return {"submission": sub_dict, "snapshots": snapshots, "growth_rates": growth_rates}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error in /api/sf/submissions/%s: %s", submission_id, e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@sf_router.get("/submissions/{submission_id}/snapshots")
def get_sf_submission_snapshots(
    submission_id: str,
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
):
    conn = get_connection()
    try:
        return {"snapshots": sf_queries.get_sf_snapshots(conn, submission_id, start, end)}
    except Exception as e:
        logger.error("Error in /api/sf/submissions/%s/snapshots: %s", submission_id, e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@sf_router.get("/aggregate")
def get_sf_aggregate(
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
    account_id: int | None = Query(None),
):
    conn = get_connection()
    try:
        return {"snapshots": sf_queries.get_sf_aggregate_snapshots(conn, start, end, account_id=account_id)}
    except Exception as e:
        logger.error("Error in /api/sf/aggregate: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@sf_router.get("/comparison")
def get_sf_comparison(
    ids: str = Query(..., description="Comma-separated submission IDs"),
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
):
    submission_ids = [x.strip() for x in ids.split(",") if x.strip()]
    if len(submission_ids) > 10:
        raise HTTPException(400, "Max 10 submissions for comparison")

    conn = get_connection()
    try:
        data = sf_queries.get_sf_comparison_snapshots(conn, submission_ids, start, end)
        titles = {}
        for sid in submission_ids:
            sub = sf_queries.get_sf_submission(conn, sid)
            if sub:
                titles[sid] = sub["title"]
        return {"series": data, "titles": titles}
    except Exception as e:
        logger.error("Error in /api/sf/comparison: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@sf_router.get("/poll_log")
def get_sf_poll_log(limit: int = Query(50, ge=1, le=200)):
    conn = get_connection()
    try:
        return {"polls": sf_queries.get_sf_poll_log(conn, limit)}
    except Exception as e:
        logger.error("Error in /api/sf/poll_log: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


# -- SF CSV Export -----------------------------------------------------

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


@sf_router.get("/export/submissions")
def export_sf_submissions():
    conn = get_connection()
    try:
        subs = sf_queries.get_all_sf_submissions(conn)
        return _csv_response(subs, "sofurry_submissions.csv")
    finally:
        conn.close()


@sf_router.get("/export/snapshots")
def export_sf_snapshots(id: str | None = Query(None)):
    conn = get_connection()
    try:
        if id:
            snaps = sf_queries.get_sf_snapshots(conn, id)
        else:
            snaps = [dict(r) for r in conn.execute("SELECT * FROM sf_snapshots ORDER BY polled_at ASC").fetchall()]
        return _csv_response(snaps, f"sofurry_snapshots{'_' + id if id else ''}.csv")
    finally:
        conn.close()
