"""REST API endpoints for the X/Twitter (TW) analytics dashboard.

X/Twitter uses internal GraphQL endpoints with cookie-based auth.
Same cookie-based scraping approach as the DeviantArt integration.
Users provide auth_token + ct0 cookies from their browser.

Stats tracked: views, likes, retweets, replies, quotes, bookmarks (6 metrics).
Tweet IDs are numeric strings (TEXT — 64-bit ints exceed JS safe range).
"""

from __future__ import annotations
import csv
import io
import logging
from typing import Optional

from fastapi import APIRouter, Query, HTTPException
from fastapi.responses import StreamingResponse

from database.db import get_connection
from database import tw_queries
from polling.tw_poller import run_tw_poll_cycle, tw_poll_progress
from polling.background import spawn_poll
from clients.tw.client import TWClient
import config

logger = logging.getLogger(__name__)
tw_router = APIRouter(prefix="/api/tw")


# -- TW Auth ------------------------------------------------------------------

@tw_router.get("/auth/status")
def tw_auth_status():
    """Check whether X/Twitter credentials are configured and whether there is any TW data."""
    settings = config.get_settings()
    # Connected for polling if EITHER the scrape cookies OR an X API token is set
    # (the official-API backend needs only the token; posting still needs cookies).
    has_credentials = bool(
        (settings.get("tw_auth_token") and settings.get("tw_ct0"))
        or settings.get("tw_api_bearer_token")
    )
    has_data = False
    conn = get_connection()
    try:
        count = conn.execute("SELECT COUNT(*) as c FROM tw_submissions").fetchone()["c"]
        has_data = count > 0
    except Exception:
        pass
    finally:
        conn.close()
    # Report which poll backend is PRIMARY (tried first) so the connect card can
    # show what's in use. Mirrors get_all_tweets' order: gallery-dl first (free),
    # then the official API (paid fallback), then the GraphQL scrape.
    from clients.tw import gallerydl, official_api
    gallerydl_available = gallerydl.find_gallerydl(settings) is not None
    if gallerydl.is_enabled(settings):
        backend = "gallerydl"
    elif official_api.is_enabled(settings):
        backend = "official"
    else:
        backend = "graphql"
    return {
        "has_credentials": has_credentials,
        "has_data": has_data,
        "username": settings.get("tw_target_user", ""),
        "poll_backend": backend,
        "gallerydl_available": gallerydl_available,
        "has_api_token": bool(settings.get("tw_api_bearer_token")),
    }


@tw_router.post("/auth/connect")
async def tw_connect(body: dict):
    """Validate X/Twitter cookies and save to settings.

    Auth flow:
      1. Receive auth_token, ct0, and target_user from the frontend
      2. Create a temporary TWClient and validate cookies
      3. If validation succeeds, save credentials to settings.json

    Cookie acquisition: Open x.com → F12 → Application → Cookies →
    copy auth_token and ct0 values.
    """
    auth_token = body.get("auth_token", "").strip()
    ct0 = body.get("ct0", "").strip()
    target_user = body.get("target_user", "").strip()

    if not auth_token:
        raise HTTPException(400, "auth_token cookie is required (F12 → Application → Cookies on x.com)")
    if not ct0:
        raise HTTPException(400, "ct0 cookie is required (F12 → Application → Cookies on x.com)")
    if not target_user:
        raise HTTPException(400, "Target user is required (the X/Twitter user to track, without @)")

    # Validate against the persistent singleton so a successful
    # validation leaves a live session in place for the next poll cycle.
    from polling.tw_poller import _get_or_create_client
    overlay = {
        **config.get_settings(),
        "tw_auth_token": auth_token,
        "tw_ct0": ct0,
        "tw_target_user": target_user,
    }
    client = _get_or_create_client(overlay, auth_token, ct0, target_user)
    try:
        valid = await client.validate_cookies()
    except Exception as e:
        raise HTTPException(502, f"Failed to validate cookies: {e}")

    if not valid:
        raise HTTPException(401, "Cookies appear invalid — could not resolve user. Check values and try again.")

    config.save_settings({
        "tw_auth_token": auth_token,
        "tw_ct0": ct0,
        "tw_target_user": target_user,
        "tw_notifications_enabled": True,
    })

    return {"status": "success", "message": f"Connected — tracking @{target_user}"}


@tw_router.post("/auth/disconnect")
def tw_disconnect():
    """Clear X/Twitter credentials from settings."""
    config.delete_settings_keys(["tw_auth_token", "tw_ct0", "tw_target_user"])
    config.save_settings({"tw_notifications_enabled": False})
    return {"status": "success", "message": "X/Twitter disconnected"}


# -- TW official API token (opt-in official-API poll backend) ------------------

@tw_router.post("/api-token/connect")
async def tw_api_token_connect(body: dict):
    """Validate + save an X API v2 **Bearer token** for the official-API poll backend.

    This is the ToS-compliant, IP-agnostic path (pay-per-use — owned reads ~$0.001
    each). Only affects POLLING; posting stays on the cookie/GraphQL path. Get a
    Bearer token from the X developer portal (developer.x.com) with a project that
    has billing enabled.
    """
    token = (body.get("bearer_token") or "").strip()
    settings = config.get_settings()
    target = (body.get("target_user") or settings.get("tw_target_user") or "").strip().lstrip("@")
    if not token:
        raise HTTPException(400, "An X API Bearer token is required (from developer.x.com)")
    if not target:
        raise HTTPException(400, "Set the X username to track first (target_user)")

    from clients.tw import official_api
    # Overlay so is_enabled() sees the not-yet-saved token during validation.
    overlay = {**settings, "tw_api_bearer_token": token, "tw_polling_backend": "official"}
    try:
        verdict = await official_api.validate(token, target, overlay)
    except Exception as e:
        raise HTTPException(502, f"Failed to validate token: {e}")
    if verdict is False:
        raise HTTPException(401, "X API rejected the token (401/403). Check the Bearer token "
                                 "and that its project/plan can read user timelines.")
    if verdict is None:
        raise HTTPException(502, "Could not reach the X API to validate the token. Try again.")

    save = {"tw_api_bearer_token": token, "tw_notifications_enabled": True}
    # Persist the target too, so a token-only user (no cookies) is fully configured.
    if not settings.get("tw_target_user"):
        save["tw_target_user"] = target
    config.save_settings(save)
    return {"status": "success",
            "message": f"X API token saved — polling @{target} via the official API"}


@tw_router.post("/api-token/disconnect")
def tw_api_token_disconnect():
    """Remove the X API token; polling falls back to gallery-dl / GraphQL scrape."""
    config.delete_settings_keys(["tw_api_bearer_token"])
    return {"status": "success",
            "message": "X API token removed — polling falls back to gallery-dl / scrape"}


# -- TW Polling ---------------------------------------------------------------

@tw_router.get("/poll/progress")
def get_tw_poll_progress():
    return dict(tw_poll_progress)


@tw_router.post("/poll/trigger")
async def trigger_tw_poll():
    """Manual poll trigger for X/Twitter."""
    try:
        spawn_poll(run_tw_poll_cycle(), "run_tw_poll_cycle")
        return {"status": "started"}
    # Let an explicit HTTPException through — the ownership guard in
    # spawn_poll raises 409 here, and the blanket handler below would
    # otherwise report it as a 500 'internal error'.
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error in TW poll trigger: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))


@tw_router.post("/poll/full-resync")
async def tw_full_resync():
    """Force full X/Twitter resync."""
    try:
        spawn_poll(run_tw_poll_cycle(force_full=True), "run_tw_poll_cycle full-resync")
        return {"status": "started"}
    # Let an explicit HTTPException through — the ownership guard in
    # spawn_poll raises 409 here, and the blanket handler below would
    # otherwise report it as a 500 'internal error'.
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error in TW full resync: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))


# -- TW Data ------------------------------------------------------------------

@tw_router.get("/status")
def get_tw_status():
    conn = get_connection()
    try:
        last_poll = tw_queries.get_tw_last_poll(conn)
        count = conn.execute("SELECT COUNT(*) as c FROM tw_submissions").fetchone()["c"]
        snap_count = conn.execute("SELECT COUNT(*) as c FROM tw_snapshots").fetchone()["c"]
        return {
            "total_submissions": count,
            "total_snapshots": snap_count,
            "last_poll": last_poll,
        }
    except Exception as e:
        logger.error("Error in /api/tw/status: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@tw_router.get("/summary")
def get_tw_summary(account_id: int | None = Query(None)):
    conn = get_connection()
    try:
        summary = tw_queries.get_tw_summary(conn, account_id=account_id)
        summary["growth_rates"] = tw_queries.get_tw_growth_rates(conn)
        return summary
    except Exception as e:
        logger.error("Error in /api/tw/summary: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@tw_router.get("/submissions")
def get_tw_submissions(
    sort_by: str = Query("views", description="Sort field"),
    order: str = Query("desc", description="Sort order"),
    search: str = Query("", description="Search title/keywords"),
    content_type: str = Query("", description="Filter by content type (tweet/reply/quote)"),
    account_id: int | None = Query(None),
):
    conn = get_connection()
    try:
        subs = tw_queries.get_all_tw_submissions(conn, sort_by=sort_by, order=order, account_id=account_id)
        deltas = tw_queries.get_tw_submission_deltas(conn)

        if search:
            search_lower = search.lower()
            subs = [s for s in subs if search_lower in s["title"].lower() or search_lower in (s.get("keywords") or "").lower()]
        if content_type:
            subs = [s for s in subs if (s.get("content_type") or "").lower() == content_type.lower()]

        for s in subs:
            d = deltas.get(s["submission_id"], {})
            s["views_delta"] = d.get("views_delta", 0)
            s["likes_delta"] = d.get("likes_delta", 0)
            s["retweets_delta"] = d.get("retweets_delta", 0)
            s["replies_delta"] = d.get("replies_delta", 0)
            s["quotes_delta"] = d.get("quotes_delta", 0)
            s["bookmarks_delta"] = d.get("bookmarks_delta", 0)

        return {"submissions": subs, "total": len(subs)}
    except Exception as e:
        logger.error("Error in /api/tw/submissions: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@tw_router.get("/submissions/{submission_id}")
def get_tw_submission(submission_id: str):
    conn = get_connection()
    try:
        sub = tw_queries.get_tw_submission(conn, submission_id)
        if not sub:
            raise HTTPException(status_code=404, detail="Tweet not found")
        snapshots = tw_queries.get_tw_snapshots(conn, submission_id)
        growth_rates = tw_queries.get_tw_submission_growth_rates(conn, submission_id)
        try:
            tags = conn.execute(
                "SELECT t.tag_id, t.name, t.color FROM tags t JOIN submission_tags st ON t.tag_id = st.tag_id WHERE st.platform = 'tw' AND st.submission_id = ?",
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
        logger.error("Error in /api/tw/submissions/%s: %s", submission_id, e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@tw_router.get("/submissions/{submission_id}/snapshots")
def get_tw_submission_snapshots(
    submission_id: str,
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
):
    conn = get_connection()
    try:
        return {"snapshots": tw_queries.get_tw_snapshots(conn, submission_id, start, end)}
    except Exception as e:
        logger.error("Error in /api/tw/submissions/%s/snapshots: %s", submission_id, e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@tw_router.get("/aggregate")
def get_tw_aggregate(
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
    account_id: int | None = Query(None),
):
    conn = get_connection()
    try:
        return {"snapshots": tw_queries.get_tw_aggregate_snapshots(conn, start, end, account_id=account_id)}
    except Exception as e:
        logger.error("Error in /api/tw/aggregate: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@tw_router.get("/comparison")
def get_tw_comparison(
    ids: str = Query(..., description="Comma-separated tweet IDs"),
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
):
    submission_ids = [x.strip() for x in ids.split(",") if x.strip()]
    if len(submission_ids) > 10:
        raise HTTPException(400, "Max 10 tweets for comparison")

    conn = get_connection()
    try:
        data = tw_queries.get_tw_comparison_snapshots(conn, submission_ids, start, end)
        titles = {}
        for sid in submission_ids:
            sub = tw_queries.get_tw_submission(conn, sid)
            if sub:
                titles[sid] = sub["title"]
        return {"series": data, "titles": titles}
    except Exception as e:
        logger.error("Error in /api/tw/comparison: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@tw_router.get("/poll_log")
def get_tw_poll_log(limit: int = Query(50, ge=1, le=200)):
    conn = get_connection()
    try:
        return {"polls": tw_queries.get_tw_poll_log(conn, limit)}
    except Exception as e:
        logger.error("Error in /api/tw/poll_log: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


# -- TW CSV Export ------------------------------------------------------------

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


@tw_router.get("/export/submissions")
def export_tw_submissions():
    conn = get_connection()
    try:
        subs = tw_queries.get_all_tw_submissions(conn)
        return _csv_response(subs, "tw_submissions.csv")
    finally:
        conn.close()


@tw_router.get("/export/snapshots")
def export_tw_snapshots(id: str | None = Query(None)):
    conn = get_connection()
    try:
        if id:
            snaps = tw_queries.get_tw_snapshots(conn, id)
        else:
            snaps = [dict(r) for r in conn.execute("SELECT * FROM tw_snapshots ORDER BY polled_at ASC").fetchall()]
        return _csv_response(snaps, f"tw_snapshots{'_' + id if id else ''}.csv")
    finally:
        conn.close()
