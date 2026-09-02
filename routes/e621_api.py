"""REST API endpoints for the e621 (E621) analytics dashboard.

Official e621 REST API, HTTP Basic auth (username + API key). Poll-only:
tracks the connected user's own uploads. Metrics: score (score.total, can be
negative), favorites_count (fav_count), comments_count (comment_count). Post IDs
are the e621 post number as TEXT. e621's CDN is hotlinkable, so — unlike Pixiv —
no thumbnail proxy is needed.
"""

from __future__ import annotations
import csv
import io
import logging
from typing import Optional

from fastapi import APIRouter, Query, HTTPException
from fastapi.responses import StreamingResponse

from database.db import get_connection
from database import e621_queries
from polling.e621_poller import run_e621_poll_cycle, e621_poll_progress
from polling.background import spawn_poll
import config

logger = logging.getLogger(__name__)
e621_router = APIRouter(prefix="/api/e621")


# -- E621 Auth ----------------------------------------------------------------

@e621_router.get("/auth/status")
def e621_auth_status():
    settings = config.get_settings()
    has_credentials = bool(settings.get("e621_username") and settings.get("e621_api_key"))
    has_data = False
    conn = get_connection()
    try:
        count = conn.execute("SELECT COUNT(*) as c FROM e621_submissions").fetchone()["c"]
        has_data = count > 0
    except Exception:
        pass
    finally:
        conn.close()
    return {
        "has_credentials": has_credentials,
        "has_data": has_data,
        "username": settings.get("e621_username", ""),
    }


@e621_router.post("/auth/connect")
async def e621_connect(body: dict):
    """Validate an e621 username + API key and save them."""
    username = str(body.get("username", "") or "").strip()
    api_key = str(body.get("api_key", "") or "").strip()

    if not username or not api_key:
        raise HTTPException(400, "Both username and API key are required (Account → Manage API Access on e621 — this is the API key, NOT your password)")

    from polling.e621_poller import _get_or_create_client
    overlay = {
        **config.get_settings(),
        "e621_username": username,
        "e621_api_key": api_key,
    }
    client = _get_or_create_client(overlay, username, api_key)
    try:
        name = await client.validate_session()
    except Exception as e:
        raise HTTPException(502, f"Failed to validate credentials: {e}")

    if not name:
        raise HTTPException(401, "Auth failed — the username or API key is wrong. Generate a key under Account → Manage API Access.")

    config.save_settings({
        "e621_username": username,
        "e621_api_key": api_key,
        "e621_notifications_enabled": True,
    })

    return {"status": "success", "message": f"Connected — tracking {name}"}


@e621_router.post("/auth/disconnect")
def e621_disconnect():
    config.delete_settings_keys(["e621_username", "e621_api_key"])
    config.save_settings({"e621_notifications_enabled": False})
    return {"status": "success", "message": "e621 disconnected"}


# -- E621 Polling -------------------------------------------------------------

@e621_router.get("/poll/progress")
def get_e621_poll_progress():
    return dict(e621_poll_progress)


@e621_router.post("/poll/trigger")
async def trigger_e621_poll():
    try:
        spawn_poll(run_e621_poll_cycle(), "run_e621_poll_cycle")
        return {"status": "started"}
    # Let an explicit HTTPException through — the ownership guard in
    # spawn_poll raises 409 here, and the blanket handler below would
    # otherwise report it as a 500 'internal error'.
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error in e621 poll trigger: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))


@e621_router.post("/poll/full-resync")
async def e621_full_resync():
    try:
        spawn_poll(run_e621_poll_cycle(force_full=True), "run_e621_poll_cycle full-resync")
        return {"status": "started"}
    # Let an explicit HTTPException through — the ownership guard in
    # spawn_poll raises 409 here, and the blanket handler below would
    # otherwise report it as a 500 'internal error'.
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error in e621 full resync: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))


# -- E621 Data ----------------------------------------------------------------

@e621_router.get("/status")
def get_e621_status():
    conn = get_connection()
    try:
        last_poll = e621_queries.get_e621_last_poll(conn)
        count = conn.execute("SELECT COUNT(*) as c FROM e621_submissions").fetchone()["c"]
        snap_count = conn.execute("SELECT COUNT(*) as c FROM e621_snapshots").fetchone()["c"]
        return {
            "total_submissions": count,
            "total_snapshots": snap_count,
            "last_poll": last_poll,
        }
    except Exception as e:
        logger.error("Error in /api/e621/status: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@e621_router.get("/summary")
def get_e621_summary(account_id: int | None = Query(None)):
    conn = get_connection()
    try:
        summary = e621_queries.get_e621_summary(conn, account_id=account_id)
        summary["growth_rates"] = e621_queries.get_e621_growth_rates(conn)
        return summary
    except Exception as e:
        logger.error("Error in /api/e621/summary: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@e621_router.get("/submissions")
def get_e621_submissions(
    sort_by: str = Query("score", description="Sort field"),
    order: str = Query("desc", description="Sort order"),
    search: str = Query("", description="Search title/keywords"),
    account_id: int | None = Query(None),
):
    conn = get_connection()
    try:
        subs = e621_queries.get_all_e621_submissions(conn, sort_by=sort_by, order=order, account_id=account_id)
        deltas = e621_queries.get_e621_submission_deltas(conn)

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
        logger.error("Error in /api/e621/submissions: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@e621_router.get("/submissions/{submission_id:path}")
def get_e621_submission(submission_id: str):
    conn = get_connection()
    try:
        sub = e621_queries.get_e621_submission(conn, submission_id)
        if not sub:
            raise HTTPException(status_code=404, detail="e621 post not found")

        full_id = sub["submission_id"]
        snapshots = e621_queries.get_e621_snapshots(conn, full_id)
        growth_rates = e621_queries.get_e621_submission_growth_rates(conn, full_id)
        try:
            tags = conn.execute(
                "SELECT t.tag_id, t.name, t.color FROM tags t JOIN submission_tags st ON t.tag_id = st.tag_id WHERE st.platform = 'e621' AND st.submission_id = ?",
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
        logger.error("Error in /api/e621/submissions/%s: %s", submission_id[:50], e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@e621_router.get("/submissions/{submission_id:path}/snapshots")
def get_e621_submission_snapshots(
    submission_id: str,
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
):
    conn = get_connection()
    try:
        return {"snapshots": e621_queries.get_e621_snapshots(conn, submission_id, start, end)}
    except Exception as e:
        logger.error("Error in /api/e621/submissions/%s/snapshots: %s", submission_id[:50], e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@e621_router.get("/aggregate")
def get_e621_aggregate(
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
    account_id: int | None = Query(None),
):
    conn = get_connection()
    try:
        return {"snapshots": e621_queries.get_e621_aggregate_snapshots(conn, start, end, account_id=account_id)}
    except Exception as e:
        logger.error("Error in /api/e621/aggregate: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@e621_router.get("/comparison")
def get_e621_comparison(
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
            sub = e621_queries.get_e621_submission(conn, rid)
            if sub:
                submission_ids.append(sub["submission_id"])
                titles[sub["submission_id"]] = sub["title"]

        data = e621_queries.get_e621_comparison_snapshots(conn, submission_ids, start, end)
        return {"series": data, "titles": titles}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error in /api/e621/comparison: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@e621_router.get("/poll_log")
def get_e621_poll_log(limit: int = Query(50, ge=1, le=200)):
    conn = get_connection()
    try:
        return {"polls": e621_queries.get_e621_poll_log(conn, limit)}
    except Exception as e:
        logger.error("Error in /api/e621/poll_log: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


# -- E621 CSV Export ----------------------------------------------------------

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


@e621_router.get("/export/submissions")
def export_e621_submissions():
    conn = get_connection()
    try:
        subs = e621_queries.get_all_e621_submissions(conn)
        return _csv_response(subs, "e621_submissions.csv")
    finally:
        conn.close()


@e621_router.get("/export/snapshots")
def export_e621_snapshots(id: str | None = Query(None)):
    conn = get_connection()
    try:
        if id:
            snaps = e621_queries.get_e621_snapshots(conn, id)
        else:
            snaps = [dict(r) for r in conn.execute("SELECT * FROM e621_snapshots ORDER BY polled_at ASC").fetchall()]
        return _csv_response(snaps, f"e621_snapshots{'_' + id[:20] if id else ''}.csv")
    finally:
        conn.close()
