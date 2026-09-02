"""REST API endpoints for the fbr (Furbooru) analytics dashboard.

Furbooru runs Philomena; its read JSON API is public. Auth is just the
username (an optional API key raises the anon rate cap / reveals own hidden
images). Poll-only: tracks the connected user's own uploads. Metrics: score
(upvotes − downvotes, can be negative), favorites_count (faves),
comments_count (comment_count). Post IDs are the Furbooru image id as TEXT.
Furbooru's CDN is hotlinkable, so — unlike Pixiv — no thumbnail proxy is needed.
"""

from __future__ import annotations
import csv
import io
import logging
from typing import Optional

from fastapi import APIRouter, Query, HTTPException
from fastapi.responses import StreamingResponse

from database.db import get_connection
from database import fbr_queries
from polling.fbr_poller import run_fbr_poll_cycle, fbr_poll_progress
from polling.background import spawn_poll
import config

logger = logging.getLogger(__name__)
fbr_router = APIRouter(prefix="/api/fbr")


# -- Furbooru Auth ----------------------------------------------------------------

@fbr_router.get("/auth/status")
def fbr_auth_status():
    settings = config.get_settings()
    has_credentials = bool(settings.get("fbr_username"))
    has_data = False
    conn = get_connection()
    try:
        count = conn.execute("SELECT COUNT(*) as c FROM fbr_submissions").fetchone()["c"]
        has_data = count > 0
    except Exception:
        pass
    finally:
        conn.close()
    return {
        "has_credentials": has_credentials,
        "has_data": has_data,
        "username": settings.get("fbr_username", ""),
    }


@fbr_router.post("/auth/connect")
async def fbr_connect(body: dict):
    """Validate a Furbooru username (API key optional) and save it.

    Furbooru runs Philomena, whose read API is public — polling your own uploads
    needs only your username. An optional API key (Account → API Key on Furbooru)
    raises rate limits and lets the poll see your own hidden images."""
    username = str(body.get("username", "") or "").strip()
    api_key = str(body.get("api_key", "") or "").strip()

    if not username:
        raise HTTPException(400, "Your Furbooru username is required (the API key is optional)")

    from polling.fbr_poller import _get_or_create_client
    overlay = {
        **config.get_settings(),
        "fbr_username": username,
        "fbr_api_key": api_key,
    }
    client = _get_or_create_client(overlay, username, api_key)
    try:
        name = await client.validate_session()
    except Exception as e:
        raise HTTPException(502, f"Failed to validate: {e}")

    if not name:
        raise HTTPException(401, "Couldn't find that Furbooru user — check the username.")

    config.save_settings({
        "fbr_username": username,
        "fbr_api_key": api_key,
        "fbr_notifications_enabled": True,
    })

    return {"status": "success", "message": f"Connected — tracking {name}"}


@fbr_router.post("/auth/disconnect")
def fbr_disconnect():
    config.delete_settings_keys(["fbr_username", "fbr_api_key"])
    config.save_settings({"fbr_notifications_enabled": False})
    return {"status": "success", "message": "fbr disconnected"}


# -- Furbooru Polling -------------------------------------------------------------

@fbr_router.get("/poll/progress")
def get_fbr_poll_progress():
    return dict(fbr_poll_progress)


@fbr_router.post("/poll/trigger")
async def trigger_fbr_poll():
    try:
        spawn_poll(run_fbr_poll_cycle(), "run_fbr_poll_cycle")
        return {"status": "started"}
    # Let an explicit HTTPException through — the ownership guard in
    # spawn_poll raises 409 here, and the blanket handler below would
    # otherwise report it as a 500 'internal error'.
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error in fbr poll trigger: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))


@fbr_router.post("/poll/full-resync")
async def fbr_full_resync():
    try:
        spawn_poll(run_fbr_poll_cycle(force_full=True), "run_fbr_poll_cycle full-resync")
        return {"status": "started"}
    # Let an explicit HTTPException through — the ownership guard in
    # spawn_poll raises 409 here, and the blanket handler below would
    # otherwise report it as a 500 'internal error'.
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error in fbr full resync: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))


# -- Furbooru Data ----------------------------------------------------------------

@fbr_router.get("/status")
def get_fbr_status():
    conn = get_connection()
    try:
        last_poll = fbr_queries.get_fbr_last_poll(conn)
        count = conn.execute("SELECT COUNT(*) as c FROM fbr_submissions").fetchone()["c"]
        snap_count = conn.execute("SELECT COUNT(*) as c FROM fbr_snapshots").fetchone()["c"]
        return {
            "total_submissions": count,
            "total_snapshots": snap_count,
            "last_poll": last_poll,
        }
    except Exception as e:
        logger.error("Error in /api/fbr/status: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@fbr_router.get("/summary")
def get_fbr_summary(account_id: int | None = Query(None)):
    conn = get_connection()
    try:
        summary = fbr_queries.get_fbr_summary(conn, account_id=account_id)
        summary["growth_rates"] = fbr_queries.get_fbr_growth_rates(conn)
        return summary
    except Exception as e:
        logger.error("Error in /api/fbr/summary: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@fbr_router.get("/submissions")
def get_fbr_submissions(
    sort_by: str = Query("score", description="Sort field"),
    order: str = Query("desc", description="Sort order"),
    search: str = Query("", description="Search title/keywords"),
    account_id: int | None = Query(None),
):
    conn = get_connection()
    try:
        subs = fbr_queries.get_all_fbr_submissions(conn, sort_by=sort_by, order=order, account_id=account_id)
        deltas = fbr_queries.get_fbr_submission_deltas(conn)

        if search:
            search_lower = search.lower()
            subs = [s for s in subs if search_lower in s["title"].lower() or search_lower in (s.get("keywords") or "").lower()]

        for s in subs:
            d = deltas.get(s["submission_id"], {})
            s["score_delta"] = d.get("score_delta", 0)
            s["favorites_delta"] = d.get("favorites_delta", 0)
            s["comments_delta"] = d.get("comments_delta", 0)

        return {"submissions": subs, "total": len(subs)}
    except Exception as e:
        logger.error("Error in /api/fbr/submissions: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@fbr_router.get("/submissions/{submission_id:path}")
def get_fbr_submission(submission_id: str):
    conn = get_connection()
    try:
        sub = fbr_queries.get_fbr_submission(conn, submission_id)
        if not sub:
            raise HTTPException(status_code=404, detail="fbr post not found")

        full_id = sub["submission_id"]
        snapshots = fbr_queries.get_fbr_snapshots(conn, full_id)
        growth_rates = fbr_queries.get_fbr_submission_growth_rates(conn, full_id)
        try:
            tags = conn.execute(
                "SELECT t.tag_id, t.name, t.color FROM tags t JOIN submission_tags st ON t.tag_id = st.tag_id WHERE st.platform = 'fbr' AND st.submission_id = ?",
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
        logger.error("Error in /api/fbr/submissions/%s: %s", submission_id[:50], e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@fbr_router.get("/submissions/{submission_id:path}/snapshots")
def get_fbr_submission_snapshots(
    submission_id: str,
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
):
    conn = get_connection()
    try:
        return {"snapshots": fbr_queries.get_fbr_snapshots(conn, submission_id, start, end)}
    except Exception as e:
        logger.error("Error in /api/fbr/submissions/%s/snapshots: %s", submission_id[:50], e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@fbr_router.get("/aggregate")
def get_fbr_aggregate(
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
    account_id: int | None = Query(None),
):
    conn = get_connection()
    try:
        return {"snapshots": fbr_queries.get_fbr_aggregate_snapshots(conn, start, end, account_id=account_id)}
    except Exception as e:
        logger.error("Error in /api/fbr/aggregate: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@fbr_router.get("/comparison")
def get_fbr_comparison(
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
            sub = fbr_queries.get_fbr_submission(conn, rid)
            if sub:
                submission_ids.append(sub["submission_id"])
                titles[sub["submission_id"]] = sub["title"]

        data = fbr_queries.get_fbr_comparison_snapshots(conn, submission_ids, start, end)
        return {"series": data, "titles": titles}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error in /api/fbr/comparison: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@fbr_router.get("/poll_log")
def get_fbr_poll_log(limit: int = Query(50, ge=1, le=200)):
    conn = get_connection()
    try:
        return {"polls": fbr_queries.get_fbr_poll_log(conn, limit)}
    except Exception as e:
        logger.error("Error in /api/fbr/poll_log: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


# -- Furbooru CSV Export ----------------------------------------------------------

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


@fbr_router.get("/export/submissions")
def export_fbr_submissions():
    conn = get_connection()
    try:
        subs = fbr_queries.get_all_fbr_submissions(conn)
        return _csv_response(subs, "fbr_submissions.csv")
    finally:
        conn.close()


@fbr_router.get("/export/snapshots")
def export_fbr_snapshots(id: str | None = Query(None)):
    conn = get_connection()
    try:
        if id:
            snaps = fbr_queries.get_fbr_snapshots(conn, id)
        else:
            snaps = [dict(r) for r in conn.execute("SELECT * FROM fbr_snapshots ORDER BY polled_at ASC").fetchall()]
        return _csv_response(snaps, f"fbr_snapshots{'_' + id[:20] if id else ''}.csv")
    finally:
        conn.close()
