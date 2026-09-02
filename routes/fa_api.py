"""REST API endpoints for the FurAffinity analytics dashboard.

This module mirrors the structure of api.py (Inkbunny routes) for consistency
across platforms, but differs in its authentication approach:

  - Inkbunny (api.py): uses username/password auth validated against the IB API
  - FurAffinity (this file): uses cookie-based auth (cookies 'a' and 'b')

FA does not provide a public API. Instead, the FAClient scrapes gallery pages
using the user's session cookies. The 'a' and 'b' cookies are the two session
cookies FA sets in the browser -- users extract these from their browser's
developer tools and provide them here.

The endpoint structure intentionally mirrors api.py so the frontend can use
the same component patterns for IB and FA dashboards, just swapping the
/api/ prefix for /api/fa/.
"""

from __future__ import annotations
import csv
import io
import logging
from typing import Optional
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Query, HTTPException
from fastapi.responses import Response, StreamingResponse

from database.db import get_connection
from database import fa_queries
from polling.fa_poller import run_fa_poll_cycle, fa_poll_progress
from polling.background import spawn_poll
from clients.fa.client import FAClient
import config

logger = logging.getLogger(__name__)
fa_router = APIRouter(prefix="/api/fa")

# Long-lived httpx client for proxying FA thumbnail requests.
# Reused across requests to benefit from connection pooling.
_fa_thumb_client = httpx.AsyncClient(timeout=15.0)


# ── FA Auth ──────────────────────────────────────────────────
# FurAffinity uses cookie-based authentication, which is fundamentally
# different from Inkbunny's username/password flow:
#
#   - IB: username + password -> validated via IB API -> session created
#   - FA: cookie_a + cookie_b -> validated by loading a gallery page ->
#         if the page loads successfully, cookies are valid
#
# The connect/disconnect pattern:
#   1. POST /auth/connect:  validate cookies, save to settings.json,
#      hot-reload config globals so the poller picks them up immediately
#   2. POST /auth/disconnect: remove cookies from settings.json,
#      clear config globals, disable FA notifications

@fa_router.get("/auth/status")
def fa_auth_status():
    """Check whether FA credentials (cookies) exist and whether there is any FA data.

    Unlike IB which checks for username/password, FA checks for the presence
    of both cookie_a and cookie_b. Also returns the saved FA username for
    display in the frontend.
    """
    settings = config.get_settings()
    has_cookies = bool(settings.get("fa_cookie_a") and settings.get("fa_cookie_b"))
    has_data = False
    conn = get_connection()
    try:
        count = conn.execute("SELECT COUNT(*) as c FROM fa_submissions").fetchone()["c"]
        has_data = count > 0
    except Exception:
        pass
    finally:
        conn.close()
    return {
        "has_cookies": has_cookies,
        "has_data": has_data,
        "username": settings.get("fa_username", ""),
    }


@fa_router.post("/auth/connect")
async def fa_connect(body: dict):
    """Validate FA cookies and save to settings.

    Auth flow (cookie-based, contrasting with IB's username/password):
      1. Receive username + cookie_a + cookie_b from the frontend
      2. Create a temporary FAClient with those cookies
      3. Call validate_cookies() which attempts to load the user's gallery page
         -- if the page loads and looks correct, cookies are valid
         -- if the page shows a login form or errors, cookies are invalid
      4. On success, persist cookies to settings.json
      5. Hot-reload config globals so the FA poller picks them up immediately
         without requiring a server restart
      6. Enable FA notifications by default on successful connection
    """
    username = body.get("username", "").strip()
    cookie_a = body.get("cookie_a", "").strip()
    cookie_b = body.get("cookie_b", "").strip()

    if not username:
        raise HTTPException(400, "FA username is required")
    if not cookie_a or not cookie_b:
        raise HTTPException(400, "Both cookie 'a' and cookie 'b' are required")

    # Validate cookies by attempting to access the user's gallery
    from polling.cf_proxy import proxy_kwargs
    client = FAClient(username=username, cookie_a=cookie_a, cookie_b=cookie_b,
                      **proxy_kwargs(config.get_settings(), "fa"))
    try:
        res = await client.validate_session()
    except Exception as e:
        raise HTTPException(502, f"Failed to validate cookies: {e}")
    finally:
        await client.close()

    # A logged-in session for the WRONG account used to sail through here and
    # be stored, because the check only asked "is anyone logged in". Saving it
    # would point polling at one account's gallery and posting at another's.
    if res["logged_in"] and not res["matches"]:
        raise HTTPException(401, res["detail"])
    if not res["ok"]:
        raise HTTPException(401, res.get("detail") or
                            "Cookies appear invalid — could not sign in. "
                            "Check the values and try again.")

    # Persist to settings.json and enable FA notifications
    config.save_settings({
        "fa_username": username,
        "fa_cookie_a": cookie_a,
        "fa_cookie_b": cookie_b,
        "fa_notifications_enabled": True,
    })

    # Hot-reload: update config module globals in-place so the FA poller
    # uses the new cookies on its next cycle without a server restart
    config.FA_USERNAME = username
    config.FA_COOKIE_A = cookie_a
    config.FA_COOKIE_B = cookie_b

    return {"status": "success", "message": f"Connected as {username}"}


@fa_router.post("/auth/disconnect")
def fa_disconnect():
    """Clear FA credentials from settings and reset config globals.

    Mirrors the IB logout pattern:
      1. Remove all FA-related keys from settings.json
      2. Disable FA notifications
      3. Clear config module globals to prevent the poller from using stale cookies
    """
    config.delete_settings_keys(["fa_username", "fa_cookie_a", "fa_cookie_b"])
    config.save_settings({"fa_notifications_enabled": False})

    # Clear config globals so the FA poller stops using stale cookies
    config.FA_USERNAME = ""
    config.FA_COOKIE_A = ""
    config.FA_COOKIE_B = ""

    return {"status": "success", "message": "FurAffinity disconnected"}


# ── FA Polling ───────────────────────────────────────────────
# Mirrors IB polling endpoints (poll/trigger, poll/full-resync, poll/progress).
# Same two-action pattern:
#   - poll/trigger:     incremental poll, only fetches changed data
#   - poll/full-resync: forces complete re-scrape of all FA submissions

@fa_router.get("/poll/progress")
def get_fa_poll_progress():
    """Return the current FA poll progress state for the frontend progress bar."""
    return dict(fa_poll_progress)


@fa_router.post("/poll/trigger")
async def trigger_fa_poll():
    """Manual 'refresh now' -- runs an incremental FA poll cycle.

    Mirrors IB's /api/poll/trigger. Only fetches data for new/changed submissions.
    """
    try:
        spawn_poll(run_fa_poll_cycle(), "run_fa_poll_cycle")
        return {"status": "started"}
    # Let an explicit HTTPException through — the ownership guard in
    # spawn_poll raises 409 here, and the blanket handler below would
    # otherwise report it as a 500 'internal error'.
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error in FA poll trigger: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))


@fa_router.post("/poll/full-resync")
async def fa_full_resync():
    """Force full FA resync -- re-scrapes all submissions regardless of changes.

    Mirrors IB's /api/poll/full-resync. Useful for recovering from data
    inconsistencies or after schema changes.
    """
    try:
        spawn_poll(run_fa_poll_cycle(force_full=True), "run_fa_poll_cycle full-resync")
        return {"status": "started"}
    # Let an explicit HTTPException through — the ownership guard in
    # spawn_poll raises 409 here, and the blanket handler below would
    # otherwise report it as a 500 'internal error'.
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error in FA full resync: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))


# ── FA Data ──────────────────────────────────────────────────
# All data endpoints mirror the IB equivalents in api.py for frontend
# consistency. The same endpoint shapes (status, summary, submissions,
# submissions/{id}, snapshots, aggregate, comparison, poll_log) are
# provided so the frontend can reuse the same components and chart logic.

@fa_router.get("/status")
def get_fa_status():
    """Polling status for FA -- mirrors IB's /api/status."""
    conn = get_connection()
    try:
        last_poll = fa_queries.get_fa_last_poll(conn)
        count = conn.execute("SELECT COUNT(*) as c FROM fa_submissions").fetchone()["c"]
        snap_count = conn.execute("SELECT COUNT(*) as c FROM fa_snapshots").fetchone()["c"]
        return {
            "total_submissions": count,
            "total_snapshots": snap_count,
            "last_poll": last_poll,
        }
    except Exception as e:
        logger.error("Error in /api/fa/status: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@fa_router.get("/summary")
def get_fa_summary(account_id: int | None = Query(None)):
    """Dashboard summary for FA -- mirrors IB's /api/summary.

    With *account_id* set, the totals / top-lists / recent activity are scoped to
    that account ("All accounts" by default). growth_rates + watcher counts +
    profile pageviews stay aggregate for now (a Phase 2 follow-up).
    """
    conn = get_connection()
    try:
        summary = fa_queries.get_fa_summary(conn, account_id=account_id)
        summary["growth_rates"] = fa_queries.get_fa_growth_rates(conn)
        summary["total_watchers"] = fa_queries.get_fa_watchers_count(conn)
        summary["recent_watchers"] = fa_queries.get_fa_recent_watchers(conn, limit=10)
        # Profile pageviews from FAExport (latest snapshot)
        profile_stats = fa_queries.get_fa_latest_profile_stats(conn)
        summary["profile_pageviews"] = profile_stats["pageviews"] if profile_stats else 0
        return summary
    except Exception as e:
        logger.error("Error in /api/fa/summary: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@fa_router.get("/submissions")
def get_fa_submissions(
    sort_by: str = Query("views", description="Sort field"),
    order: str = Query("desc", description="Sort order"),
    search: str = Query("", description="Search title/keywords"),
    rating: str = Query("", description="Filter by rating"),
    account_id: int | None = Query(None),
):
    """All FA submissions with latest stats, sortable/filterable.

    Mirrors IB's /api/submissions. Note: FA does not have a type_name filter
    (unlike IB which distinguishes picture/writing/etc.) since FA categorises
    content differently. With *account_id* set, results are scoped to that account.
    """
    conn = get_connection()
    try:
        subs = fa_queries.get_all_fa_submissions(conn, sort_by=sort_by, order=order, account_id=account_id)
        # Get per-submission deltas (change since last poll)
        deltas = fa_queries.get_fa_submission_deltas(conn)

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
        logger.error("Error in /api/fa/submissions: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@fa_router.get("/submissions/{submission_id}")
def get_fa_submission(submission_id: int):
    """Full detail for a single FA submission.

    Mirrors IB's /api/submissions/{id}. Note: FA does not have a faving_users
    list (FA does not expose who faved a submission), so only submission metadata,
    snapshots, comments, and growth rates are returned.
    """
    conn = get_connection()
    try:
        sub = fa_queries.get_fa_submission(conn, submission_id)
        if not sub:
            raise HTTPException(status_code=404, detail="FA submission not found")
        snapshots = fa_queries.get_fa_snapshots(conn, submission_id)
        comments = fa_queries.get_fa_comments(conn, submission_id)
        growth_rates = fa_queries.get_fa_submission_growth_rates(conn, submission_id)
        try:
            tags = conn.execute(
                "SELECT t.tag_id, t.name, t.color FROM tags t JOIN submission_tags st ON t.tag_id = st.tag_id WHERE st.platform = 'fa' AND st.submission_id = ?",
                (submission_id,),
            ).fetchall()
        except Exception:
            tags = []
        sub_dict = dict(sub) if not isinstance(sub, dict) else sub
        sub_dict["tags"] = [dict(r) for r in tags]
        return {"submission": sub_dict, "snapshots": snapshots, "comments": comments, "growth_rates": growth_rates}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error in /api/fa/submissions/%d: %s", submission_id, e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@fa_router.get("/submissions/{submission_id}/snapshots")
def get_fa_submission_snapshots(
    submission_id: int,
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
):
    """Time-series data for a single FA submission -- mirrors IB's snapshots endpoint."""
    conn = get_connection()
    try:
        return {"snapshots": fa_queries.get_fa_snapshots(conn, submission_id, start, end)}
    except Exception as e:
        logger.error("Error in /api/fa/submissions/%d/snapshots: %s", submission_id, e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@fa_router.get("/aggregate")
def get_fa_aggregate(
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
    account_id: int | None = Query(None),
):
    """Aggregate time-series across all FA submissions -- mirrors IB's /api/aggregate.
    With *account_id* set, the totals are scoped to that account."""
    conn = get_connection()
    try:
        return {"snapshots": fa_queries.get_fa_aggregate_snapshots(conn, start, end, account_id=account_id)}
    except Exception as e:
        logger.error("Error in /api/fa/aggregate: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@fa_router.get("/comparison")
def get_fa_comparison(
    ids: str = Query(..., description="Comma-separated submission IDs"),
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
):
    """Multi-submission comparison for FA -- mirrors IB's /api/comparison.

    Same 10-submission cap as the IB endpoint for consistent frontend behaviour.
    """
    try:
        submission_ids = [int(x.strip()) for x in ids.split(",") if x.strip()]
    except ValueError:
        raise HTTPException(400, "Invalid submission IDs")
    if len(submission_ids) > 10:
        raise HTTPException(400, "Max 10 submissions for comparison")

    conn = get_connection()
    try:
        data = fa_queries.get_fa_comparison_snapshots(conn, submission_ids, start, end)
        titles = {}
        for sid in submission_ids:
            sub = fa_queries.get_fa_submission(conn, sid)
            if sub:
                titles[sid] = sub["title"]
        # Convert int keys to string keys for JSON serialisation compatibility
        return {"series": {str(k): v for k, v in data.items()}, "titles": {str(k): v for k, v in titles.items()}}
    except Exception as e:
        logger.error("Error in /api/fa/comparison: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@fa_router.get("/watchers")
def get_fa_watchers():
    """Recent FA watchers list with total count."""
    conn = get_connection()
    try:
        watchers = fa_queries.get_fa_recent_watchers(conn, limit=50)
        count = fa_queries.get_fa_watchers_count(conn)
        return {"watchers": watchers, "total": count}
    finally:
        conn.close()


@fa_router.get("/poll_log")
def get_fa_poll_log(limit: int = Query(50, ge=1, le=200)):
    """Recent FA poll history -- mirrors IB's /api/poll_log."""
    conn = get_connection()
    try:
        return {"polls": fa_queries.get_fa_poll_log(conn, limit)}
    except Exception as e:
        logger.error("Error in /api/fa/poll_log: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


# ── FA CSV Export ─────────────────────────────────────────────
# Mirrors IB's CSV export pattern: DictWriter -> StringIO -> StreamingResponse.
# Same helper function structure as api.py for consistency.

def _sanitize_csv_value(val):
    """Prevent CSV formula injection — prefix dangerous chars with single quote."""
    if isinstance(val, str) and val and val[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + val
    return val


def _csv_response(rows: list[dict], filename: str) -> StreamingResponse:
    """Generate a CSV StreamingResponse from a list of dicts.

    Same DictWriter -> StreamingResponse pattern as the IB module.
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


@fa_router.get("/export/submissions")
def export_fa_submissions():
    """Export all FA submissions as a CSV file download."""
    conn = get_connection()
    try:
        subs = fa_queries.get_all_fa_submissions(conn)
        return _csv_response(subs, "furaffinity_submissions.csv")
    finally:
        conn.close()


@fa_router.get("/export/snapshots")
def export_fa_snapshots(id: int | None = Query(None)):
    """Export FA snapshots as CSV. If an ID is provided, export only that submission's
    snapshots; otherwise export all snapshots across all submissions."""
    conn = get_connection()
    try:
        if id:
            snaps = fa_queries.get_fa_snapshots(conn, id)
        else:
            snaps = [dict(r) for r in conn.execute("SELECT * FROM fa_snapshots ORDER BY polled_at ASC").fetchall()]
        return _csv_response(snaps, f"fa_snapshots{'_' + str(id) if id else ''}.csv")
    finally:
        conn.close()


# ── FA Thumbnail Proxy ───────────────────────────────────────
# Same CORS bypass pattern as IB's /api/thumb, but with a different domain
# whitelist. FA serves images from furaffinity.net and facdn.net domains.
# Both must be allowed since FA uses different CDN subdomains for thumbnails
# vs full images.

@fa_router.get("/thumb")
async def proxy_fa_thumbnail(url: str = Query(..., description="FA thumbnail URL")):
    """Proxy FA thumbnails to avoid cross-origin blocking.

    Domain whitelist: only furaffinity.net and facdn.net are allowed.
    This covers FA's two CDN domains:
      - furaffinity.net: main site and some image URLs
      - facdn.net: FA's dedicated CDN for thumbnails and images
    Any other domain is rejected to prevent open proxy abuse.
    Responses are cached for 24 hours via Cache-Control header.
    """
    parsed = urlparse(url)
    allowed = ("furaffinity.net", "facdn.net")
    # Check if the hostname ends with one of the allowed domains
    # (covers subdomains like t.facdn.net, www.furaffinity.net, etc.)
    if not parsed.hostname or not any(
        parsed.hostname == d or parsed.hostname.endswith("." + d) for d in allowed
    ):
        raise HTTPException(400, "Only FurAffinity CDN URLs allowed")
    try:
        resp = await _fa_thumb_client.get(url)
        resp.raise_for_status()
        content_type = resp.headers.get("content-type", "image/jpeg")
        return Response(content=resp.content, media_type=content_type,
                        headers={"Cache-Control": "public, max-age=86400"})
    except Exception as e:
        logger.warning("FA thumb proxy failed for %s: %s", url, e)
        raise HTTPException(502, detail="Failed to fetch thumbnail")
