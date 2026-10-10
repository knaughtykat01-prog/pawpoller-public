"""REST API endpoints for Rule34.xxx (spec 028 US6): tracking only, never posts.

Connect with the account's Rule34 username, API key and user id (Rule34 → My Account → Options → API Access
Credentials; required on every request since August 2025). Metrics: score and comment counts only.
"""

from __future__ import annotations
import csv
import io
import logging
from typing import Optional

from fastapi import APIRouter, Query, HTTPException
from fastapi.responses import StreamingResponse

from database.db import get_connection
from database import r34_queries
from polling.r34_poller import run_r34_poll_cycle, r34_poll_progress
from polling.background import spawn_poll
import config

logger = logging.getLogger(__name__)
r34_router = APIRouter(prefix="/api/r34")


# -- Rule34 Auth ----------------------------------------------------------------


@r34_router.get("/auth/status")
def r34_auth_status():
    settings = config.get_settings()
    has_credentials = bool(settings.get("r34_api_key") and settings.get("r34_user_id"))
    has_data = False
    conn = get_connection()
    try:
        has_data = conn.execute("SELECT COUNT(*) as c FROM r34_submissions").fetchone()["c"] > 0
    except Exception:
        pass
    finally:
        conn.close()
    return {"has_credentials": has_credentials, "has_data": has_data,
            "username": settings.get("r34_username", ""), "user_id": settings.get("r34_user_id", "")}


@r34_router.post("/auth/connect")
async def r34_connect(body: dict):
    """Check a Rule34 API key + user id against Rule34 and save them with the username."""
    username = str(body.get("username", "") or "").strip()
    api_key = str(body.get("api_key", "") or "").strip()
    user_id = str(body.get("user_id", "") or "").strip()
    if not (username and api_key and user_id):
        raise HTTPException(400, "Rule34 needs your username, API key and user id. The key and id are on "
                                 "Rule34: My Account → Options → API Access Credentials.")
    if not user_id.isdigit():
        raise HTTPException(400, "The user id is a number (Rule34: My Account → Options → API Access Credentials).")
    from clients.r34.client import Rule34Client
    async with Rule34Client(username, api_key, user_id) as client:
        try:
            name = await client.validate_session()
        except Exception as e:
            raise HTTPException(502, f"Couldn't reach Rule34: {e}")
    if not name:
        raise HTTPException(401, "Rule34 refused that API key and user id. Copy both again from Rule34: "
                                 "My Account → Options → API Access Credentials.")
    config.save_settings({"r34_username": username, "r34_api_key": api_key, "r34_user_id": user_id,
                          "r34_notifications_enabled": True})
    return {"status": "success", "message": f"Connected — tracking {username}"}


@r34_router.post("/auth/disconnect")
def r34_disconnect():
    config.delete_settings_keys(["r34_username", "r34_api_key", "r34_user_id"])
    config.save_settings({"r34_notifications_enabled": False})
    return {"status": "success", "message": "Rule34 disconnected"}


# -- Rule34 Polling -------------------------------------------------------------


@r34_router.get("/poll/progress")
def get_r34_poll_progress():
    return dict(r34_poll_progress)


@r34_router.post("/poll/trigger")
async def trigger_r34_poll():
    try:
        spawn_poll(run_r34_poll_cycle(), "run_r34_poll_cycle")
        return {"status": "started"}
    # Let an explicit HTTPException through — the ownership guard in
    # spawn_poll raises 409 here, and the blanket handler below would
    # otherwise report it as a 500 'internal error'.
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error in r34 poll trigger: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))


@r34_router.post("/poll/full-resync")
async def r34_full_resync():
    try:
        spawn_poll(run_r34_poll_cycle(force_full=True), "run_r34_poll_cycle full-resync")
        return {"status": "started"}
    # Let an explicit HTTPException through — the ownership guard in
    # spawn_poll raises 409 here, and the blanket handler below would
    # otherwise report it as a 500 'internal error'.
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error in r34 full resync: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))


# -- Rule34 Data ----------------------------------------------------------------


@r34_router.get("/status")
def get_r34_status():
    conn = get_connection()
    try:
        last_poll = r34_queries.get_r34_last_poll(conn)
        count = conn.execute("SELECT COUNT(*) as c FROM r34_submissions").fetchone()["c"]
        snap_count = conn.execute("SELECT COUNT(*) as c FROM r34_snapshots").fetchone()["c"]
        return {
            "total_submissions": count,
            "total_snapshots": snap_count,
            "last_poll": last_poll,
        }
    except Exception as e:
        logger.error("Error in /api/r34/status: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@r34_router.get("/summary")
def get_r34_summary(account_id: int | None = Query(None)):
    conn = get_connection()
    try:
        summary = r34_queries.get_r34_summary(conn, account_id=account_id)
        summary["growth_rates"] = r34_queries.get_r34_growth_rates(conn)
        from polling import board_track
        found = board_track.found_summary(conn, "r34", account_id)
        if found["posts"]:
            summary["found"] = found      # others' uploads kept out of the totals (spec 028)
        return summary
    except Exception as e:
        logger.error("Error in /api/r34/summary: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@r34_router.get("/submissions")
def get_r34_submissions(
    sort_by: str = Query("score", description="Sort field"),
    order: str = Query("desc", description="Sort order"),
    search: str = Query("", description="Search title/keywords"),
    account_id: int | None = Query(None),
    uploaded_by: str = Query("", description="me | others | found (spec 028)"),
):
    conn = get_connection()
    try:
        if uploaded_by == "found":
            sql, args = "SELECT * FROM r34_found", []
            if account_id:
                sql, args = sql + " WHERE account_id = ?", [account_id]
            subs = [dict(r) for r in conn.execute(sql + " ORDER BY submission_id DESC", args)]
        else:
            subs = r34_queries.get_all_r34_submissions(conn, sort_by=sort_by, order=order, account_id=account_id)
        if uploaded_by in ("me", "others"):
            subs = [s for s in subs if bool(s.get("uploaded_by_me", 1)) == (uploaded_by == "me")]
        from polling import board_track
        board_track.add_labels(conn, subs)
        deltas = r34_queries.get_r34_submission_deltas(conn)

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
        logger.error("Error in /api/r34/submissions: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


# Registered BEFORE the bare /submissions/{id} route below. The `:path`
# converter is greedy and Starlette matches in registration order, so with
# the bare route first this URL resolved to a submission id of
# "<id>/snapshots" and returned 404 for every post that exists.
@r34_router.get("/submissions/{submission_id:path}/snapshots")
def get_r34_submission_snapshots(
    submission_id: str,
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
):
    conn = get_connection()
    try:
        return {"snapshots": r34_queries.get_r34_snapshots(conn, submission_id, start, end)}
    except Exception as e:
        logger.error("Error in /api/r34/submissions/%s/snapshots: %s", submission_id[:50], e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@r34_router.get("/submissions/{submission_id:path}")
def get_r34_submission(submission_id: str):
    conn = get_connection()
    try:
        sub = r34_queries.get_r34_submission(conn, submission_id)
        if not sub:
            raise HTTPException(status_code=404, detail="r34 post not found")

        full_id = sub["submission_id"]
        snapshots = r34_queries.get_r34_snapshots(conn, full_id)
        growth_rates = r34_queries.get_r34_submission_growth_rates(conn, full_id)
        try:
            tags = conn.execute(
                "SELECT t.tag_id, t.name, t.color FROM tags t JOIN submission_tags st ON t.tag_id = st.tag_id WHERE st.platform = 'r34' AND st.submission_id = ?",
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
        logger.error("Error in /api/r34/submissions/%s: %s", submission_id[:50], e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@r34_router.get("/aggregate")
def get_r34_aggregate(
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
    account_id: int | None = Query(None),
):
    conn = get_connection()
    try:
        return {"snapshots": r34_queries.get_r34_aggregate_snapshots(conn, start, end, account_id=account_id)}
    except Exception as e:
        logger.error("Error in /api/r34/aggregate: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@r34_router.get("/comparison")
def get_r34_comparison(
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
            sub = r34_queries.get_r34_submission(conn, rid)
            if sub:
                submission_ids.append(sub["submission_id"])
                titles[sub["submission_id"]] = sub["title"]

        data = r34_queries.get_r34_comparison_snapshots(conn, submission_ids, start, end)
        return {"series": data, "titles": titles}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error in /api/r34/comparison: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@r34_router.get("/poll_log")
def get_r34_poll_log(limit: int = Query(50, ge=1, le=200)):
    conn = get_connection()
    try:
        return {"polls": r34_queries.get_r34_poll_log(conn, limit)}
    except Exception as e:
        logger.error("Error in /api/r34/poll_log: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


# -- Rule34 CSV Export ----------------------------------------------------------

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


@r34_router.get("/export/submissions")
def export_r34_submissions():
    conn = get_connection()
    try:
        subs = r34_queries.get_all_r34_submissions(conn)
        return _csv_response(subs, "r34_submissions.csv")
    finally:
        conn.close()


@r34_router.get("/export/snapshots")
def export_r34_snapshots(id: str | None = Query(None)):
    conn = get_connection()
    try:
        if id:
            snaps = r34_queries.get_r34_snapshots(conn, id)
        else:
            snaps = [dict(r) for r in conn.execute("SELECT * FROM r34_snapshots ORDER BY polled_at ASC").fetchall()]
        return _csv_response(snaps, f"r34_snapshots{'_' + id[:20] if id else ''}.csv")
    finally:
        conn.close()
