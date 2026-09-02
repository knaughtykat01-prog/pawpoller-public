"""REST API endpoints for the Bluesky (BSKY) analytics dashboard.

Bluesky provides a free public API via the AT Protocol. Authentication
uses app passwords (identifier + app_password) to obtain JWT sessions.

Tracks likes, reposts, replies, and quotes. NO views metric available.
Post IDs are AT URIs — the API accepts rkey (last segment) and resolves
by suffix match.
"""

from __future__ import annotations
import csv
import io
import logging
from typing import Optional

from fastapi import APIRouter, Query, HTTPException
from fastapi.responses import StreamingResponse

from database.db import get_connection
from database import bsky_queries
from polling.bsky_poller import run_bsky_poll_cycle, bsky_poll_progress
from polling.background import spawn_poll
from clients.bsky.client import BskyClient
import config

logger = logging.getLogger(__name__)
bsky_router = APIRouter(prefix="/api/bsky")


# -- BSKY Auth ----------------------------------------------------------------

@bsky_router.get("/auth/status")
def bsky_auth_status():
    """Check whether Bluesky credentials are configured and whether there is any BSKY data."""
    settings = config.get_settings()
    has_credentials = bool(settings.get("bsky_identifier") and settings.get("bsky_app_password"))
    has_data = False
    conn = get_connection()
    try:
        count = conn.execute("SELECT COUNT(*) as c FROM bsky_submissions").fetchone()["c"]
        has_data = count > 0
    except Exception:
        pass
    finally:
        conn.close()
    return {
        "has_credentials": has_credentials,
        "has_data": has_data,
        "username": settings.get("bsky_identifier", ""),
    }


@bsky_router.post("/auth/connect")
async def bsky_connect(body: dict):
    """Validate Bluesky credentials and save to settings.

    Auth flow:
      1. Receive identifier + app_password from the frontend
      2. Create a temporary BskyClient and validate the session
      3. If valid, save credentials to settings.json
    """
    identifier = body.get("identifier", "").strip()
    app_password = body.get("app_password", "").strip()

    if not identifier:
        raise HTTPException(400, "Identifier is required (handle like user.bsky.social or DID)")
    if not app_password:
        raise HTTPException(400, "App password is required (generate one at bsky.app/settings)")

    # Validate against the persistent singleton so a successful login
    # leaves a live session in place for the next poll cycle to reuse,
    # instead of validating + closing + forcing the next call to relogin.
    from polling.bsky_poller import _get_or_create_client
    overlay = {
        **config.get_settings(),
        "bsky_identifier": identifier,
        "bsky_app_password": app_password,
    }
    client = _get_or_create_client(overlay, identifier, app_password)
    try:
        handle = await client.validate_session()
    except Exception as e:
        raise HTTPException(502, f"Failed to validate credentials: {e}")

    if not handle:
        raise HTTPException(401, "Login failed — check identifier and app password. Use an App Password, not your account password.")

    config.save_settings({
        "bsky_identifier": identifier,
        "bsky_app_password": app_password,
        "bsky_notifications_enabled": True,
    })

    return {"status": "success", "message": f"Connected — tracking {handle}"}


@bsky_router.post("/auth/disconnect")
def bsky_disconnect():
    """Clear Bluesky credentials from settings."""
    config.delete_settings_keys(["bsky_identifier", "bsky_app_password"])
    config.save_settings({"bsky_notifications_enabled": False})
    return {"status": "success", "message": "Bluesky disconnected"}


# -- BSKY Polling -------------------------------------------------------------

@bsky_router.get("/poll/progress")
def get_bsky_poll_progress():
    return dict(bsky_poll_progress)


@bsky_router.post("/poll/trigger")
async def trigger_bsky_poll():
    """Manual poll trigger for Bluesky."""
    try:
        spawn_poll(run_bsky_poll_cycle(), "run_bsky_poll_cycle")
        return {"status": "started"}
    # Let an explicit HTTPException through — the ownership guard in
    # spawn_poll raises 409 here, and the blanket handler below would
    # otherwise report it as a 500 'internal error'.
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error in BSKY poll trigger: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))


@bsky_router.post("/poll/full-resync")
async def bsky_full_resync():
    """Force full Bluesky resync."""
    try:
        spawn_poll(run_bsky_poll_cycle(force_full=True), "run_bsky_poll_cycle full-resync")
        return {"status": "started"}
    # Let an explicit HTTPException through — the ownership guard in
    # spawn_poll raises 409 here, and the blanket handler below would
    # otherwise report it as a 500 'internal error'.
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error in BSKY full resync: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))


# -- BSKY Data ----------------------------------------------------------------

@bsky_router.get("/status")
def get_bsky_status():
    conn = get_connection()
    try:
        last_poll = bsky_queries.get_bsky_last_poll(conn)
        count = conn.execute("SELECT COUNT(*) as c FROM bsky_submissions").fetchone()["c"]
        snap_count = conn.execute("SELECT COUNT(*) as c FROM bsky_snapshots").fetchone()["c"]
        return {
            "total_submissions": count,
            "total_snapshots": snap_count,
            "last_poll": last_poll,
        }
    except Exception as e:
        logger.error("Error in /api/bsky/status: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@bsky_router.get("/summary")
def get_bsky_summary(account_id: int | None = Query(None)):
    """Dashboard summary: totals, top liked/reposted, fastest growing, growth rates.

    With *account_id* set, totals + top-lists scope to that account ("All
    accounts" by default). growth_rates stays aggregate for now (mirrors IB).
    """
    conn = get_connection()
    try:
        summary = bsky_queries.get_bsky_summary(conn, account_id=account_id)
        summary["growth_rates"] = bsky_queries.get_bsky_growth_rates(conn)
        return summary
    except Exception as e:
        logger.error("Error in /api/bsky/summary: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@bsky_router.get("/submissions")
def get_bsky_submissions(
    sort_by: str = Query("likes", description="Sort field"),
    order: str = Query("desc", description="Sort order"),
    search: str = Query("", description="Search title/keywords"),
    account_id: int | None = Query(None),
):
    conn = get_connection()
    try:
        subs = bsky_queries.get_all_bsky_submissions(conn, sort_by=sort_by, order=order, account_id=account_id)
        deltas = bsky_queries.get_bsky_submission_deltas(conn)

        if search:
            search_lower = search.lower()
            subs = [s for s in subs if search_lower in s["title"].lower() or search_lower in (s.get("keywords") or "").lower()]

        for s in subs:
            d = deltas.get(s["submission_id"], {})
            s["likes_delta"] = d.get("likes_delta", 0)
            s["reposts_delta"] = d.get("reposts_delta", 0)
            s["replies_delta"] = d.get("replies_delta", 0)
            s["quotes_delta"] = d.get("quotes_delta", 0)

        return {"submissions": subs, "total": len(subs)}
    except Exception as e:
        logger.error("Error in /api/bsky/submissions: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@bsky_router.get("/submissions/{submission_id:path}")
def get_bsky_submission(submission_id: str):
    conn = get_connection()
    try:
        # Try exact match first, then rkey suffix match
        sub = bsky_queries.get_bsky_submission(conn, submission_id)
        if not sub:
            sub = bsky_queries.get_bsky_submission_by_rkey(conn, submission_id)
        if not sub:
            raise HTTPException(status_code=404, detail="Bluesky post not found")

        full_id = sub["submission_id"]
        snapshots = bsky_queries.get_bsky_snapshots(conn, full_id)
        growth_rates = bsky_queries.get_bsky_submission_growth_rates(conn, full_id)
        try:
            tags = conn.execute(
                "SELECT t.tag_id, t.name, t.color FROM tags t JOIN submission_tags st ON t.tag_id = st.tag_id WHERE st.platform = 'bsky' AND st.submission_id = ?",
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
        logger.error("Error in /api/bsky/submissions/%s: %s", submission_id[:50], e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@bsky_router.get("/submissions/{submission_id:path}/snapshots")
def get_bsky_submission_snapshots(
    submission_id: str,
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
):
    conn = get_connection()
    try:
        # Resolve rkey if needed
        sub = bsky_queries.get_bsky_submission(conn, submission_id)
        if not sub:
            sub = bsky_queries.get_bsky_submission_by_rkey(conn, submission_id)
        full_id = sub["submission_id"] if sub else submission_id
        return {"snapshots": bsky_queries.get_bsky_snapshots(conn, full_id, start, end)}
    except Exception as e:
        logger.error("Error in /api/bsky/submissions/%s/snapshots: %s", submission_id[:50], e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@bsky_router.get("/aggregate")
def get_bsky_aggregate(
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
    account_id: int | None = Query(None),
):
    conn = get_connection()
    try:
        return {"snapshots": bsky_queries.get_bsky_aggregate_snapshots(conn, start, end, account_id=account_id)}
    except Exception as e:
        logger.error("Error in /api/bsky/aggregate: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@bsky_router.get("/comparison")
def get_bsky_comparison(
    ids: str = Query(..., description="Comma-separated post rkeys or URIs"),
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
):
    conn = get_connection()
    try:
        # Resolve rkeys to full URIs
        raw_ids = [x.strip() for x in ids.split(",") if x.strip()]
        if len(raw_ids) > 10:
            raise HTTPException(400, "Max 10 posts for comparison")

        submission_ids = []
        titles = {}
        for rid in raw_ids:
            sub = bsky_queries.get_bsky_submission(conn, rid)
            if not sub:
                sub = bsky_queries.get_bsky_submission_by_rkey(conn, rid)
            if sub:
                full_id = sub["submission_id"]
                submission_ids.append(full_id)
                titles[full_id] = sub["title"]

        data = bsky_queries.get_bsky_comparison_snapshots(conn, submission_ids, start, end)
        return {"series": data, "titles": titles}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error in /api/bsky/comparison: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@bsky_router.get("/poll_log")
def get_bsky_poll_log(limit: int = Query(50, ge=1, le=200)):
    conn = get_connection()
    try:
        return {"polls": bsky_queries.get_bsky_poll_log(conn, limit)}
    except Exception as e:
        logger.error("Error in /api/bsky/poll_log: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


# -- BSKY CSV Export ----------------------------------------------------------

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


@bsky_router.get("/export/submissions")
def export_bsky_submissions():
    conn = get_connection()
    try:
        subs = bsky_queries.get_all_bsky_submissions(conn)
        return _csv_response(subs, "bsky_submissions.csv")
    finally:
        conn.close()


@bsky_router.get("/export/snapshots")
def export_bsky_snapshots(id: str | None = Query(None)):
    conn = get_connection()
    try:
        if id:
            # Resolve rkey if needed
            sub = bsky_queries.get_bsky_submission(conn, id)
            if not sub:
                sub = bsky_queries.get_bsky_submission_by_rkey(conn, id)
            full_id = sub["submission_id"] if sub else id
            snaps = bsky_queries.get_bsky_snapshots(conn, full_id)
        else:
            snaps = [dict(r) for r in conn.execute("SELECT * FROM bsky_snapshots ORDER BY polled_at ASC").fetchall()]
        return _csv_response(snaps, f"bsky_snapshots{'_' + id[:20] if id else ''}.csv")
    finally:
        conn.close()
