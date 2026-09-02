"""REST API endpoints for the Weasyl analytics dashboard.

This module mirrors the structure of api.py (Inkbunny) and fa_api.py (FurAffinity)
for frontend consistency, but uses Weasyl's simpler API key authentication:

  - Inkbunny (api.py):   username + password, validated against IB's login API
  - FurAffinity (fa_api.py): cookie 'a' + cookie 'b', validated by loading a page
  - Weasyl (this file):  single API key, validated via Weasyl's /api/whoami endpoint

Weasyl has a proper public API, so authentication is the simplest of the three
platforms -- just an API key that the user generates in their Weasyl account settings.

Key differences from IB and FA:
  - No comments endpoint: Weasyl's API does not expose comments on submissions,
    so get_ws_submission() does not return a "comments" field
  - No faving_users endpoint: Weasyl's API does not expose who faved a submission,
    so get_ws_submission() does not return a "faving_users" field
  - No thumbnail proxy: Weasyl sets proper CORS headers on its CDN, so no proxy
    is needed (unlike IB's metapix.net and FA's facdn.net which block cross-origin)

The endpoint structure intentionally mirrors api.py and fa_api.py so the frontend
can reuse the same dashboard components across all three platforms, just swapping
the /api/ prefix for /api/ws/.
"""

from __future__ import annotations
import csv
import io
import logging
from typing import Optional

from fastapi import APIRouter, Query, HTTPException
from fastapi.responses import StreamingResponse

from database.db import get_connection
from database import ws_queries
from polling.ws_poller import run_ws_poll_cycle, ws_poll_progress
from polling.background import spawn_poll
from clients.weasyl.client import WeasylClient
import config

logger = logging.getLogger(__name__)
ws_router = APIRouter(prefix="/api/ws")


# ── WS Auth ──────────────────────────────────────────────────
# Weasyl uses API key authentication -- the simplest auth model of the
# three platforms. Users generate an API key in their Weasyl account
# settings page and provide it here. No cookies or passwords needed.
#
# The connect/disconnect pattern mirrors FA's approach:
#   1. POST /auth/connect:    validate key via Weasyl's /api/whoami,
#      save to settings.json, hot-reload is not needed since the poller
#      reads from settings each cycle
#   2. POST /auth/disconnect: remove key from settings.json

@ws_router.get("/auth/status")
def ws_auth_status():
    """Check whether a Weasyl API key exists and whether there is any WS data.

    Unlike IB (username/password) or FA (cookies a+b), Weasyl only needs
    a single API key. The response also includes the saved username for
    frontend display.
    """
    settings = config.get_settings()
    has_key = bool(settings.get("ws_api_key"))
    has_data = False
    conn = get_connection()
    try:
        count = conn.execute("SELECT COUNT(*) as c FROM ws_submissions").fetchone()["c"]
        has_data = count > 0
    except Exception:
        pass
    finally:
        conn.close()
    return {
        "has_key": has_key,
        "has_data": has_data,
        "username": settings.get("ws_username", ""),
    }


@ws_router.post("/auth/connect")
async def ws_connect(body: dict):
    """Validate Weasyl API key and save to settings.

    Auth flow (API key -- simpler than IB's password or FA's cookies):
      1. Receive the API key from the frontend
      2. Create a temporary WeasylClient and call validate_key()
      3. validate_key() calls Weasyl's /api/whoami endpoint with the key
         in the X-Weasyl-API-Key header
      4. If the key is valid, Weasyl returns the username; if invalid,
         it returns an error
      5. On success, save the key and discovered username to settings.json
      6. Enable WS notifications by default on successful connection
    """
    api_key = body.get("api_key", "").strip()

    if not api_key:
        raise HTTPException(400, "Weasyl API key is required")

    # Validate the API key by calling Weasyl's /api/whoami endpoint
    from polling.cf_proxy import proxy_kwargs
    client = WeasylClient(api_key=api_key,
                          **proxy_kwargs(config.get_settings(), "ws"))
    try:
        username = await client.validate_key()
    except Exception as e:
        raise HTTPException(502, f"Failed to validate API key: {e}")
    finally:
        await client.close()

    if not username:
        raise HTTPException(401, "API key appears invalid — could not authenticate. Check the key and try again.")

    # Persist to settings.json and enable WS notifications
    config.save_settings({
        "ws_api_key": api_key,
        "ws_username": username,
        "ws_notifications_enabled": True,
    })

    return {"status": "success", "message": f"Connected as {username}"}


@ws_router.post("/auth/disconnect")
def ws_disconnect():
    """Clear Weasyl credentials from settings.

    Mirrors the FA disconnect pattern: remove all WS-related keys from
    settings.json and disable WS notifications.
    """
    config.delete_settings_keys(["ws_api_key", "ws_username"])
    config.save_settings({"ws_notifications_enabled": False})

    return {"status": "success", "message": "Weasyl disconnected"}


# ── WS Polling ───────────────────────────────────────────────
# Mirrors IB and FA polling endpoints (poll/trigger, poll/full-resync,
# poll/progress). Same two-action pattern as the other platforms.

@ws_router.get("/poll/progress")
def get_ws_poll_progress():
    """Return the current WS poll progress state for the frontend progress bar."""
    return dict(ws_poll_progress)


@ws_router.post("/poll/trigger")
async def trigger_ws_poll():
    """Manual 'refresh now' -- runs an incremental Weasyl poll cycle.

    Mirrors IB's /api/poll/trigger and FA's /api/fa/poll/trigger.
    """
    try:
        spawn_poll(run_ws_poll_cycle(), "run_ws_poll_cycle")
        return {"status": "started"}
    # Let an explicit HTTPException through — the ownership guard in
    # spawn_poll raises 409 here, and the blanket handler below would
    # otherwise report it as a 500 'internal error'.
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error in WS poll trigger: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))


@ws_router.post("/poll/full-resync")
async def ws_full_resync():
    """Force full Weasyl resync -- re-fetches all submissions regardless of changes.

    Mirrors IB and FA full-resync endpoints.
    """
    try:
        spawn_poll(run_ws_poll_cycle(force_full=True), "run_ws_poll_cycle full-resync")
        return {"status": "started"}
    # Let an explicit HTTPException through — the ownership guard in
    # spawn_poll raises 409 here, and the blanket handler below would
    # otherwise report it as a 500 'internal error'.
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error in WS full resync: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))


# ── WS Data ──────────────────────────────────────────────────
# All data endpoints mirror the IB and FA equivalents for frontend
# consistency. The same endpoint shapes are provided:
#   - /status, /summary, /submissions, /submissions/{id},
#     /submissions/{id}/snapshots, /aggregate, /comparison, /poll_log
#
# Notable omissions compared to IB:
#   - No faving_users: Weasyl's API does not expose who faved a submission
#   - No comments: Weasyl's API does not expose submission comments
# These limitations mean get_ws_submission() returns fewer fields than
# the IB equivalent, and the frontend hides those tabs on the WS detail page.

@ws_router.get("/status")
def get_ws_status():
    """Polling status for Weasyl -- mirrors IB's /api/status."""
    conn = get_connection()
    try:
        last_poll = ws_queries.get_ws_last_poll(conn)
        count = conn.execute("SELECT COUNT(*) as c FROM ws_submissions").fetchone()["c"]
        snap_count = conn.execute("SELECT COUNT(*) as c FROM ws_snapshots").fetchone()["c"]
        return {
            "total_submissions": count,
            "total_snapshots": snap_count,
            "last_poll": last_poll,
        }
    except Exception as e:
        logger.error("Error in /api/ws/status: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@ws_router.get("/summary")
def get_ws_summary(account_id: int | None = Query(None)):
    """Dashboard summary for Weasyl -- mirrors IB's /api/summary.

    With *account_id* set, the totals / top-lists are scoped to that account
    ("All accounts" by default). growth_rates stays aggregate for now (mirrors IB).
    """
    conn = get_connection()
    try:
        summary = ws_queries.get_ws_summary(conn, account_id=account_id)
        summary["growth_rates"] = ws_queries.get_ws_growth_rates(conn)
        return summary
    except Exception as e:
        logger.error("Error in /api/ws/summary: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@ws_router.get("/submissions")
def get_ws_submissions(
    sort_by: str = Query("views", description="Sort field"),
    order: str = Query("desc", description="Sort order"),
    search: str = Query("", description="Search title/keywords"),
    rating: str = Query("", description="Filter by rating"),
    account_id: int | None = Query(None),
):
    """All Weasyl submissions with latest stats, sortable/filterable.

    Mirrors IB's /api/submissions and FA's /api/fa/submissions.
    Like FA, no type_name filter since Weasyl categorises content differently.
    With *account_id* set, the list is scoped to that account.
    """
    conn = get_connection()
    try:
        subs = ws_queries.get_all_ws_submissions(conn, sort_by=sort_by, order=order, account_id=account_id)
        # Get per-submission deltas (change since last poll)
        deltas = ws_queries.get_ws_submission_deltas(conn)

        # In-memory filtering for search text and rating
        if search:
            search_lower = search.lower()
            subs = [s for s in subs if search_lower in s["title"].lower() or search_lower in (s.get("keywords") or "").lower()]
        if rating:
            subs = [s for s in subs if (s.get("rating") or "").lower() == rating.lower()]

        # Merge delta values into each submission for the frontend
        for s in subs:
            d = deltas.get(s["submission_id"], {})
            s["views_delta"] = d.get("views_delta", 0)
            s["faves_delta"] = d.get("faves_delta", 0)
            s["comments_delta"] = d.get("comments_delta", 0)

        return {"submissions": subs, "total": len(subs)}
    except Exception as e:
        logger.error("Error in /api/ws/submissions: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@ws_router.get("/submissions/{submission_id}")
def get_ws_submission(submission_id: int):
    """Full detail for a single Weasyl submission.

    Mirrors IB's /api/submissions/{id}, but with fewer fields due to
    Weasyl API limitations:
      - No "faving_users": Weasyl's API does not expose who faved a submission
      - No "comments": Weasyl's API does not expose submission comments
    Only submission metadata, snapshots, and growth rates are returned.
    The frontend hides the faving users and comments tabs on the WS detail page.
    """
    conn = get_connection()
    try:
        sub = ws_queries.get_ws_submission(conn, submission_id)
        if not sub:
            raise HTTPException(status_code=404, detail="WS submission not found")
        snapshots = ws_queries.get_ws_snapshots(conn, submission_id)
        growth_rates = ws_queries.get_ws_submission_growth_rates(conn, submission_id)
        try:
            tags = conn.execute(
                "SELECT t.tag_id, t.name, t.color FROM tags t JOIN submission_tags st ON t.tag_id = st.tag_id WHERE st.platform = 'ws' AND st.submission_id = ?",
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
        logger.error("Error in /api/ws/submissions/%d: %s", submission_id, e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@ws_router.get("/submissions/{submission_id}/snapshots")
def get_ws_submission_snapshots(
    submission_id: int,
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
):
    """Time-series data for a single Weasyl submission -- mirrors IB's snapshots endpoint."""
    conn = get_connection()
    try:
        return {"snapshots": ws_queries.get_ws_snapshots(conn, submission_id, start, end)}
    except Exception as e:
        logger.error("Error in /api/ws/submissions/%d/snapshots: %s", submission_id, e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@ws_router.get("/aggregate")
def get_ws_aggregate(
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
    account_id: int | None = Query(None),
):
    """Aggregate time-series across all Weasyl submissions -- mirrors IB's /api/aggregate.

    With *account_id* set, the totals are scoped to that account.
    """
    conn = get_connection()
    try:
        return {"snapshots": ws_queries.get_ws_aggregate_snapshots(conn, start, end, account_id=account_id)}
    except Exception as e:
        logger.error("Error in /api/ws/aggregate: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@ws_router.get("/comparison")
def get_ws_comparison(
    ids: str = Query(..., description="Comma-separated submission IDs"),
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
):
    """Multi-submission comparison for Weasyl -- mirrors IB's /api/comparison.

    Same 10-submission cap as IB and FA for consistent frontend behaviour.
    """
    try:
        submission_ids = [int(x.strip()) for x in ids.split(",") if x.strip()]
    except ValueError:
        raise HTTPException(400, "Invalid submission IDs")
    if len(submission_ids) > 10:
        raise HTTPException(400, "Max 10 submissions for comparison")

    conn = get_connection()
    try:
        data = ws_queries.get_ws_comparison_snapshots(conn, submission_ids, start, end)
        titles = {}
        for sid in submission_ids:
            sub = ws_queries.get_ws_submission(conn, sid)
            if sub:
                titles[sid] = sub["title"]
        # Convert int keys to string keys for JSON serialisation compatibility
        return {"series": {str(k): v for k, v in data.items()}, "titles": {str(k): v for k, v in titles.items()}}
    except Exception as e:
        logger.error("Error in /api/ws/comparison: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@ws_router.get("/poll_log")
def get_ws_poll_log(limit: int = Query(50, ge=1, le=200)):
    """Recent Weasyl poll history -- mirrors IB's /api/poll_log."""
    conn = get_connection()
    try:
        return {"polls": ws_queries.get_ws_poll_log(conn, limit)}
    except Exception as e:
        logger.error("Error in /api/ws/poll_log: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


# ── WS CSV Export ─────────────────────────────────────────────
# Mirrors IB and FA CSV export: DictWriter -> StringIO -> StreamingResponse.
# No thumbnail proxy is needed for Weasyl because Weasyl's CDN serves
# images with proper CORS headers, unlike IB (metapix.net) and FA (facdn.net).

def _sanitize_csv_value(val):
    """Prevent CSV formula injection — prefix dangerous chars with single quote."""
    if isinstance(val, str) and val and val[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + val
    return val


def _csv_response(rows: list[dict], filename: str) -> StreamingResponse:
    """Generate a CSV StreamingResponse from a list of dicts.

    Same DictWriter -> StreamingResponse pattern as the IB and FA modules.
    Values are sanitised against CSV formula injection before writing.
    """
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


@ws_router.get("/export/submissions")
def export_ws_submissions():
    """Export all Weasyl submissions as a CSV file download."""
    conn = get_connection()
    try:
        subs = ws_queries.get_all_ws_submissions(conn)
        return _csv_response(subs, "weasyl_submissions.csv")
    finally:
        conn.close()


@ws_router.get("/export/snapshots")
def export_ws_snapshots(id: int | None = Query(None)):
    """Export Weasyl snapshots as CSV. If an ID is provided, export only that
    submission's snapshots; otherwise export all snapshots across all submissions."""
    conn = get_connection()
    try:
        if id:
            snaps = ws_queries.get_ws_snapshots(conn, id)
        else:
            snaps = [dict(r) for r in conn.execute("SELECT * FROM ws_snapshots ORDER BY polled_at ASC").fetchall()]
        return _csv_response(snaps, f"weasyl_snapshots{'_' + str(id) if id else ''}.csv")
    finally:
        conn.close()
