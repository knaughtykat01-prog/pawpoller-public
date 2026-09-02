"""REST API endpoints for the Tumblr (TUM) analytics dashboard.

Read-only polling via the Tumblr v2 API using the app's OAuth consumer key
(api_key) + a blog identifier.

Tracks a single engagement metric: notes (likes + reblogs + replies combined).
Post IDs are numeric id_strings.
"""

from __future__ import annotations
import csv
import io
import logging
from typing import Optional

from fastapi import APIRouter, Query, HTTPException
from fastapi.responses import StreamingResponse

from database.db import get_connection
from database import tum_queries
from polling.tum_poller import run_tum_poll_cycle, tum_poll_progress
from polling.background import spawn_poll
import config

logger = logging.getLogger(__name__)
tum_router = APIRouter(prefix="/api/tum")


# -- TUM Auth -----------------------------------------------------------------

@tum_router.get("/auth/status")
def tum_auth_status():
    settings = config.get_settings()
    has_credentials = bool(settings.get("tum_api_key") and settings.get("tum_blog"))
    has_data = False
    conn = get_connection()
    try:
        count = conn.execute("SELECT COUNT(*) as c FROM tum_submissions").fetchone()["c"]
        has_data = count > 0
    except Exception:
        pass
    finally:
        conn.close()
    return {
        "has_credentials": has_credentials,
        "has_data": has_data,
        "username": settings.get("tum_blog", ""),
    }


@tum_router.post("/auth/connect")
async def tum_connect(body: dict):
    """Validate Tumblr credentials (api_key + blog) and save to settings."""
    api_key = body.get("api_key", "").strip()
    blog = body.get("blog", "").strip()

    if not api_key:
        raise HTTPException(400, "API key is required (OAuth Consumer Key from tumblr.com/oauth/apps)")
    if not blog:
        raise HTTPException(400, "Blog identifier is required (e.g. staff or staff.tumblr.com)")

    from polling.tum_poller import _get_or_create_client
    overlay = {
        **config.get_settings(),
        "tum_api_key": api_key,
        "tum_blog": blog,
    }
    client = _get_or_create_client(overlay, api_key, blog)
    try:
        name = await client.validate_session()
    except Exception as e:
        raise HTTPException(502, f"Failed to validate credentials: {e}")

    if not name:
        raise HTTPException(401, "Lookup failed — check the API key and blog identifier. The key is the app's OAuth Consumer Key.")

    config.save_settings({
        "tum_api_key": api_key,
        "tum_blog": client.blog,
        "tum_notifications_enabled": True,
    })

    return {"status": "success", "message": f"Connected — tracking {name}"}


@tum_router.post("/auth/disconnect")
def tum_disconnect():
    config.delete_settings_keys(["tum_api_key", "tum_blog"])
    config.save_settings({"tum_notifications_enabled": False})
    return {"status": "success", "message": "Tumblr disconnected"}


# -- TUM Polling --------------------------------------------------------------

@tum_router.get("/poll/progress")
def get_tum_poll_progress():
    return dict(tum_poll_progress)


@tum_router.post("/poll/trigger")
async def trigger_tum_poll():
    try:
        spawn_poll(run_tum_poll_cycle(), "run_tum_poll_cycle")
        return {"status": "started"}
    # Let an explicit HTTPException through — the ownership guard in
    # spawn_poll raises 409 here, and the blanket handler below would
    # otherwise report it as a 500 'internal error'.
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error in TUM poll trigger: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))


@tum_router.post("/poll/full-resync")
async def tum_full_resync():
    try:
        spawn_poll(run_tum_poll_cycle(force_full=True), "run_tum_poll_cycle full-resync")
        return {"status": "started"}
    # Let an explicit HTTPException through — the ownership guard in
    # spawn_poll raises 409 here, and the blanket handler below would
    # otherwise report it as a 500 'internal error'.
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error in TUM full resync: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))


# -- TUM Data -----------------------------------------------------------------

@tum_router.get("/status")
def get_tum_status():
    conn = get_connection()
    try:
        last_poll = tum_queries.get_tum_last_poll(conn)
        count = conn.execute("SELECT COUNT(*) as c FROM tum_submissions").fetchone()["c"]
        snap_count = conn.execute("SELECT COUNT(*) as c FROM tum_snapshots").fetchone()["c"]
        return {
            "total_submissions": count,
            "total_snapshots": snap_count,
            "last_poll": last_poll,
        }
    except Exception as e:
        logger.error("Error in /api/tum/status: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@tum_router.get("/summary")
def get_tum_summary(account_id: int | None = Query(None)):
    conn = get_connection()
    try:
        summary = tum_queries.get_tum_summary(conn, account_id=account_id)
        summary["growth_rates"] = tum_queries.get_tum_growth_rates(conn)
        return summary
    except Exception as e:
        logger.error("Error in /api/tum/summary: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@tum_router.get("/submissions")
def get_tum_submissions(
    sort_by: str = Query("notes", description="Sort field"),
    order: str = Query("desc", description="Sort order"),
    search: str = Query("", description="Search title/keywords"),
    account_id: int | None = Query(None),
):
    conn = get_connection()
    try:
        subs = tum_queries.get_all_tum_submissions(conn, sort_by=sort_by, order=order, account_id=account_id)
        deltas = tum_queries.get_tum_submission_deltas(conn)

        if search:
            search_lower = search.lower()
            subs = [s for s in subs if search_lower in s["title"].lower() or search_lower in (s.get("keywords") or "").lower()]

        for s in subs:
            d = deltas.get(s["submission_id"], {})
            s["notes_delta"] = d.get("notes_delta", 0)

        return {"submissions": subs, "total": len(subs)}
    except Exception as e:
        logger.error("Error in /api/tum/submissions: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@tum_router.get("/submissions/{submission_id:path}")
def get_tum_submission(submission_id: str):
    conn = get_connection()
    try:
        sub = tum_queries.get_tum_submission(conn, submission_id)
        if not sub:
            raise HTTPException(status_code=404, detail="Tumblr post not found")

        full_id = sub["submission_id"]
        snapshots = tum_queries.get_tum_snapshots(conn, full_id)
        growth_rates = tum_queries.get_tum_submission_growth_rates(conn, full_id)
        try:
            tags = conn.execute(
                "SELECT t.tag_id, t.name, t.color FROM tags t JOIN submission_tags st ON t.tag_id = st.tag_id WHERE st.platform = 'tum' AND st.submission_id = ?",
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
        logger.error("Error in /api/tum/submissions/%s: %s", submission_id[:50], e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@tum_router.get("/submissions/{submission_id:path}/snapshots")
def get_tum_submission_snapshots(
    submission_id: str,
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
):
    conn = get_connection()
    try:
        return {"snapshots": tum_queries.get_tum_snapshots(conn, submission_id, start, end)}
    except Exception as e:
        logger.error("Error in /api/tum/submissions/%s/snapshots: %s", submission_id[:50], e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@tum_router.get("/aggregate")
def get_tum_aggregate(
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
    account_id: int | None = Query(None),
):
    conn = get_connection()
    try:
        return {"snapshots": tum_queries.get_tum_aggregate_snapshots(conn, start, end, account_id=account_id)}
    except Exception as e:
        logger.error("Error in /api/tum/aggregate: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@tum_router.get("/comparison")
def get_tum_comparison(
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
            sub = tum_queries.get_tum_submission(conn, rid)
            if sub:
                submission_ids.append(sub["submission_id"])
                titles[sub["submission_id"]] = sub["title"]

        data = tum_queries.get_tum_comparison_snapshots(conn, submission_ids, start, end)
        return {"series": data, "titles": titles}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error in /api/tum/comparison: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@tum_router.get("/poll_log")
def get_tum_poll_log(limit: int = Query(50, ge=1, le=200)):
    conn = get_connection()
    try:
        return {"polls": tum_queries.get_tum_poll_log(conn, limit)}
    except Exception as e:
        logger.error("Error in /api/tum/poll_log: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


# -- TUM CSV Export -----------------------------------------------------------

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


@tum_router.get("/export/submissions")
def export_tum_submissions():
    conn = get_connection()
    try:
        subs = tum_queries.get_all_tum_submissions(conn)
        return _csv_response(subs, "tum_submissions.csv")
    finally:
        conn.close()


@tum_router.get("/export/snapshots")
def export_tum_snapshots(id: str | None = Query(None)):
    conn = get_connection()
    try:
        if id:
            snaps = tum_queries.get_tum_snapshots(conn, id)
        else:
            snaps = [dict(r) for r in conn.execute("SELECT * FROM tum_snapshots ORDER BY polled_at ASC").fetchall()]
        return _csv_response(snaps, f"tum_snapshots{'_' + id[:20] if id else ''}.csv")
    finally:
        conn.close()
