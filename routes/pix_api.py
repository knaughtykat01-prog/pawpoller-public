"""REST API endpoints for the Pixiv (PIX) analytics dashboard.

Reverse-engineered app-API (OAuth via a refresh token + optional target
user_id). Tracks gallery metrics: views, favorites_count (bookmarks),
comments_count. Work IDs are namespaced ("illust:123" / "novel:123").
"""

from __future__ import annotations
import csv
import io
import logging
from typing import Optional
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Query, HTTPException
from fastapi.responses import StreamingResponse, Response

from database.db import get_connection
from database import pix_queries
from polling.pix_poller import run_pix_poll_cycle, pix_poll_progress
from polling.background import spawn_poll
import config

logger = logging.getLogger(__name__)
pix_router = APIRouter(prefix="/api/pix")

# Pixiv's image CDN (i.pximg.net) rejects hotlinks without a pixiv Referer, so
# thumbnails MUST be proxied through the backend with that header injected.
_pix_thumb_client = httpx.AsyncClient(
    timeout=20.0,
    headers={"Referer": "https://www.pixiv.net/", "User-Agent": "PixivIOSApp/7.13.3"},
)


@pix_router.get("/thumb")
async def proxy_pix_thumbnail(url: str = Query(..., description="Pixiv pximg URL")):
    """Proxy Pixiv thumbnails — i.pximg.net 403s without a pixiv Referer.
    Domain-whitelisted to pximg.net to prevent open-proxy abuse."""
    parsed = urlparse(url)
    if not parsed.hostname or not (parsed.hostname == "pximg.net" or parsed.hostname.endswith(".pximg.net")):
        raise HTTPException(400, "Only Pixiv CDN (pximg.net) URLs allowed")
    try:
        resp = await _pix_thumb_client.get(url)
        resp.raise_for_status()
        return Response(content=resp.content,
                        media_type=resp.headers.get("content-type", "image/jpeg"),
                        headers={"Cache-Control": "public, max-age=86400"})
    except Exception as e:
        logger.warning("PIX thumb proxy failed for %s: %s", url, e)
        raise HTTPException(502, "Failed to fetch thumbnail")


# -- PIX Auth -----------------------------------------------------------------

@pix_router.get("/auth/status")
def pix_auth_status():
    settings = config.get_settings()
    has_credentials = bool(settings.get("pix_refresh_token"))
    has_data = False
    conn = get_connection()
    try:
        count = conn.execute("SELECT COUNT(*) as c FROM pix_submissions").fetchone()["c"]
        has_data = count > 0
    except Exception:
        pass
    finally:
        conn.close()
    return {
        "has_credentials": has_credentials,
        "has_data": has_data,
        "username": settings.get("pix_user_id", ""),
    }


@pix_router.post("/auth/connect")
async def pix_connect(body: dict):
    """Validate a Pixiv refresh token (and optional target user_id) and save it."""
    refresh_token = body.get("refresh_token", "").strip()
    user_id = str(body.get("user_id", "") or "").strip()

    if not refresh_token:
        raise HTTPException(400, "Refresh token is required (obtain one via a Pixiv browser login, e.g. the gppt helper)")

    from polling.pix_poller import _get_or_create_client
    overlay = {
        **config.get_settings(),
        "pix_refresh_token": refresh_token,
        "pix_user_id": user_id,
    }
    client = _get_or_create_client(overlay, refresh_token, user_id)
    try:
        name = await client.validate_session()
    except Exception as e:
        raise HTTPException(502, f"Failed to validate credentials: {e}")

    if not name:
        raise HTTPException(401, "Auth failed — the refresh token is invalid or expired. Generate a new one via a Pixiv login.")

    config.save_settings({
        # the client may have rotated the refresh token during auth
        "pix_refresh_token": client.refresh_token,
        "pix_user_id": client.user_id,
        "pix_notifications_enabled": True,
    })

    return {"status": "success", "message": f"Connected — tracking {name}"}


@pix_router.post("/auth/disconnect")
def pix_disconnect():
    config.delete_settings_keys(["pix_refresh_token", "pix_user_id"])
    config.save_settings({"pix_notifications_enabled": False})
    return {"status": "success", "message": "Pixiv disconnected"}


# -- PIX Polling --------------------------------------------------------------

@pix_router.get("/poll/progress")
def get_pix_poll_progress():
    return dict(pix_poll_progress)


@pix_router.post("/poll/trigger")
async def trigger_pix_poll():
    try:
        spawn_poll(run_pix_poll_cycle(), "run_pix_poll_cycle")
        return {"status": "started"}
    # Let an explicit HTTPException through — the ownership guard in
    # spawn_poll raises 409 here, and the blanket handler below would
    # otherwise report it as a 500 'internal error'.
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error in PIX poll trigger: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))


@pix_router.post("/poll/full-resync")
async def pix_full_resync():
    try:
        spawn_poll(run_pix_poll_cycle(force_full=True), "run_pix_poll_cycle full-resync")
        return {"status": "started"}
    # Let an explicit HTTPException through — the ownership guard in
    # spawn_poll raises 409 here, and the blanket handler below would
    # otherwise report it as a 500 'internal error'.
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error in PIX full resync: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))


# -- PIX Data -----------------------------------------------------------------

@pix_router.get("/status")
def get_pix_status():
    conn = get_connection()
    try:
        last_poll = pix_queries.get_pix_last_poll(conn)
        count = conn.execute("SELECT COUNT(*) as c FROM pix_submissions").fetchone()["c"]
        snap_count = conn.execute("SELECT COUNT(*) as c FROM pix_snapshots").fetchone()["c"]
        return {
            "total_submissions": count,
            "total_snapshots": snap_count,
            "last_poll": last_poll,
        }
    except Exception as e:
        logger.error("Error in /api/pix/status: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@pix_router.get("/summary")
def get_pix_summary(account_id: int | None = Query(None)):
    conn = get_connection()
    try:
        summary = pix_queries.get_pix_summary(conn, account_id=account_id)
        summary["growth_rates"] = pix_queries.get_pix_growth_rates(conn)
        return summary
    except Exception as e:
        logger.error("Error in /api/pix/summary: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@pix_router.get("/submissions")
def get_pix_submissions(
    sort_by: str = Query("views", description="Sort field"),
    order: str = Query("desc", description="Sort order"),
    search: str = Query("", description="Search title/keywords"),
    account_id: int | None = Query(None),
):
    conn = get_connection()
    try:
        subs = pix_queries.get_all_pix_submissions(conn, sort_by=sort_by, order=order, account_id=account_id)
        deltas = pix_queries.get_pix_submission_deltas(conn)

        if search:
            search_lower = search.lower()
            subs = [s for s in subs if search_lower in s["title"].lower() or search_lower in (s.get("keywords") or "").lower()]

        for s in subs:
            d = deltas.get(s["submission_id"], {})
            s["views_delta"] = d.get("views_delta", 0)
            s["favorites_delta"] = d.get("favorites_delta", 0)
            s["comments_delta"] = d.get("comments_delta", 0)

        return {"submissions": subs, "total": len(subs)}
    except Exception as e:
        logger.error("Error in /api/pix/submissions: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@pix_router.get("/submissions/{submission_id:path}")
def get_pix_submission(submission_id: str):
    conn = get_connection()
    try:
        sub = pix_queries.get_pix_submission(conn, submission_id)
        if not sub:
            raise HTTPException(status_code=404, detail="Pixiv work not found")

        full_id = sub["submission_id"]
        snapshots = pix_queries.get_pix_snapshots(conn, full_id)
        growth_rates = pix_queries.get_pix_submission_growth_rates(conn, full_id)
        try:
            tags = conn.execute(
                "SELECT t.tag_id, t.name, t.color FROM tags t JOIN submission_tags st ON t.tag_id = st.tag_id WHERE st.platform = 'pix' AND st.submission_id = ?",
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
        logger.error("Error in /api/pix/submissions/%s: %s", submission_id[:50], e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@pix_router.get("/submissions/{submission_id:path}/snapshots")
def get_pix_submission_snapshots(
    submission_id: str,
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
):
    conn = get_connection()
    try:
        return {"snapshots": pix_queries.get_pix_snapshots(conn, submission_id, start, end)}
    except Exception as e:
        logger.error("Error in /api/pix/submissions/%s/snapshots: %s", submission_id[:50], e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@pix_router.get("/aggregate")
def get_pix_aggregate(
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
    account_id: int | None = Query(None),
):
    conn = get_connection()
    try:
        return {"snapshots": pix_queries.get_pix_aggregate_snapshots(conn, start, end, account_id=account_id)}
    except Exception as e:
        logger.error("Error in /api/pix/aggregate: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@pix_router.get("/comparison")
def get_pix_comparison(
    ids: str = Query(..., description="Comma-separated work ids"),
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
):
    conn = get_connection()
    try:
        raw_ids = [x.strip() for x in ids.split(",") if x.strip()]
        if len(raw_ids) > 10:
            raise HTTPException(400, "Max 10 works for comparison")

        submission_ids = []
        titles = {}
        for rid in raw_ids:
            sub = pix_queries.get_pix_submission(conn, rid)
            if sub:
                submission_ids.append(sub["submission_id"])
                titles[sub["submission_id"]] = sub["title"]

        data = pix_queries.get_pix_comparison_snapshots(conn, submission_ids, start, end)
        return {"series": data, "titles": titles}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error in /api/pix/comparison: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@pix_router.get("/poll_log")
def get_pix_poll_log(limit: int = Query(50, ge=1, le=200)):
    conn = get_connection()
    try:
        return {"polls": pix_queries.get_pix_poll_log(conn, limit)}
    except Exception as e:
        logger.error("Error in /api/pix/poll_log: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


# -- PIX CSV Export -----------------------------------------------------------

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


@pix_router.get("/export/submissions")
def export_pix_submissions():
    conn = get_connection()
    try:
        subs = pix_queries.get_all_pix_submissions(conn)
        return _csv_response(subs, "pix_submissions.csv")
    finally:
        conn.close()


@pix_router.get("/export/snapshots")
def export_pix_snapshots(id: str | None = Query(None)):
    conn = get_connection()
    try:
        if id:
            snaps = pix_queries.get_pix_snapshots(conn, id)
        else:
            snaps = [dict(r) for r in conn.execute("SELECT * FROM pix_snapshots ORDER BY polled_at ASC").fetchall()]
        return _csv_response(snaps, f"pix_snapshots{'_' + id[:20] if id else ''}.csv")
    finally:
        conn.close()
