"""REST API endpoints for the Mastodon (MAST) analytics dashboard.

Mastodon is decentralised — every instance runs the same open REST API.
Authentication uses a per-instance personal access token (instance_url +
access_token; scope: read).

Tracks likes (favourites), reposts (reblogs), replies. NO quote metric.
Post IDs are ActivityPub URIs — the API accepts rkey (last segment) and
resolves by suffix match.
"""

from __future__ import annotations
import csv
import io
import logging
from typing import Optional

from fastapi import APIRouter, Query, HTTPException
from fastapi.responses import StreamingResponse

from database.db import get_connection
from database import mast_queries
from polling.mast_poller import run_mast_poll_cycle, mast_poll_progress
from polling.background import spawn_poll
from clients.mast.client import MastClient
import config

logger = logging.getLogger(__name__)
mast_router = APIRouter(prefix="/api/mast")


# -- MAST Auth ----------------------------------------------------------------

@mast_router.get("/auth/status")
def mast_auth_status():
    """Check whether Mastodon credentials are configured and whether there is any MAST data."""
    settings = config.get_settings()
    has_credentials = bool(settings.get("mast_instance_url") and settings.get("mast_access_token"))
    has_data = False
    conn = get_connection()
    try:
        count = conn.execute("SELECT COUNT(*) as c FROM mast_submissions").fetchone()["c"]
        has_data = count > 0
    except Exception:
        pass
    finally:
        conn.close()
    return {
        "has_credentials": has_credentials,
        "has_data": has_data,
        "username": settings.get("mast_instance_url", ""),
        "flavour": settings.get("mast_instance_flavour", ""),
    }


@mast_router.post("/auth/connect")
async def mast_connect(body: dict):
    """Validate Mastodon credentials and save to settings.

    Auth flow:
      1. Receive instance_url + access_token from the frontend
      2. Create a temporary MastClient and validate the token
      3. If valid, save credentials to settings.json
    """
    instance_url = body.get("instance_url", "").strip()
    access_token = body.get("access_token", "").strip()

    if not instance_url:
        raise HTTPException(400, "Instance URL is required (e.g. https://mastodon.social or pawb.fun)")
    if not access_token:
        raise HTTPException(400, "Access token is required (Settings → Development → New application, scope: read)")

    # Validate against the persistent singleton so a successful login leaves a
    # live session in place for the next poll cycle to reuse.
    from polling.mast_poller import _get_or_create_client
    overlay = {
        **config.get_settings(),
        "mast_instance_url": instance_url,
        "mast_access_token": access_token,
    }
    client = _get_or_create_client(overlay, instance_url, access_token)
    try:
        handle = await client.validate_session()
    except Exception as e:
        raise HTTPException(502, f"Failed to validate credentials: {e}")

    if not handle:
        raise HTTPException(401, "Login failed — check the instance URL and access token. The token needs at least the 'read' scope.")

    # Detect the server software (Mastodon / Pleroma / Akkoma / GoToSocial /
    # Pixelfed / …) so the UI can label the connection. Best-effort — a failure
    # here must not block an otherwise-valid connect.
    flavour = ""
    try:
        flavour = (await client.get_instance_info()).get("software", "")
    except Exception:
        pass

    config.save_settings({
        "mast_instance_url": client.instance_url,
        "mast_access_token": access_token,
        "mast_notifications_enabled": True,
        "mast_instance_flavour": flavour,
    })

    msg = f"Connected — tracking {handle}"
    if flavour and flavour != "Mastodon":
        msg += f" · {flavour}"
    return {"status": "success", "message": msg}


@mast_router.post("/auth/disconnect")
def mast_disconnect():
    """Clear Mastodon credentials from settings."""
    config.delete_settings_keys(["mast_instance_url", "mast_access_token", "mast_instance_flavour"])
    config.save_settings({"mast_notifications_enabled": False})
    return {"status": "success", "message": "Mastodon disconnected"}


# -- MAST Polling -------------------------------------------------------------

@mast_router.get("/poll/progress")
def get_mast_poll_progress():
    return dict(mast_poll_progress)


@mast_router.post("/poll/trigger")
async def trigger_mast_poll():
    """Manual poll trigger for Mastodon."""
    try:
        spawn_poll(run_mast_poll_cycle(), "run_mast_poll_cycle")
        return {"status": "started"}
    # Let an explicit HTTPException through — the ownership guard in
    # spawn_poll raises 409 here, and the blanket handler below would
    # otherwise report it as a 500 'internal error'.
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error in MAST poll trigger: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))


@mast_router.post("/poll/full-resync")
async def mast_full_resync():
    """Force full Mastodon resync."""
    try:
        spawn_poll(run_mast_poll_cycle(force_full=True), "run_mast_poll_cycle full-resync")
        return {"status": "started"}
    # Let an explicit HTTPException through — the ownership guard in
    # spawn_poll raises 409 here, and the blanket handler below would
    # otherwise report it as a 500 'internal error'.
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error in MAST full resync: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))


# -- MAST Data ----------------------------------------------------------------

@mast_router.get("/status")
def get_mast_status():
    conn = get_connection()
    try:
        last_poll = mast_queries.get_mast_last_poll(conn)
        count = conn.execute("SELECT COUNT(*) as c FROM mast_submissions").fetchone()["c"]
        snap_count = conn.execute("SELECT COUNT(*) as c FROM mast_snapshots").fetchone()["c"]
        return {
            "total_submissions": count,
            "total_snapshots": snap_count,
            "last_poll": last_poll,
        }
    except Exception as e:
        logger.error("Error in /api/mast/status: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@mast_router.get("/summary")
def get_mast_summary(account_id: int | None = Query(None)):
    """Dashboard summary: totals, top liked/reposted, fastest growing, growth rates."""
    conn = get_connection()
    try:
        summary = mast_queries.get_mast_summary(conn, account_id=account_id)
        summary["growth_rates"] = mast_queries.get_mast_growth_rates(conn)
        return summary
    except Exception as e:
        logger.error("Error in /api/mast/summary: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@mast_router.get("/submissions")
def get_mast_submissions(
    sort_by: str = Query("likes", description="Sort field"),
    order: str = Query("desc", description="Sort order"),
    search: str = Query("", description="Search title/keywords"),
    account_id: int | None = Query(None),
):
    conn = get_connection()
    try:
        subs = mast_queries.get_all_mast_submissions(conn, sort_by=sort_by, order=order, account_id=account_id)
        deltas = mast_queries.get_mast_submission_deltas(conn)

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
        logger.error("Error in /api/mast/submissions: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@mast_router.get("/submissions/{submission_id:path}")
def get_mast_submission(submission_id: str):
    conn = get_connection()
    try:
        # Try exact match first, then rkey suffix match
        sub = mast_queries.get_mast_submission(conn, submission_id)
        if not sub:
            sub = mast_queries.get_mast_submission_by_rkey(conn, submission_id)
        if not sub:
            raise HTTPException(status_code=404, detail="Mastodon status not found")

        full_id = sub["submission_id"]
        snapshots = mast_queries.get_mast_snapshots(conn, full_id)
        growth_rates = mast_queries.get_mast_submission_growth_rates(conn, full_id)
        try:
            tags = conn.execute(
                "SELECT t.tag_id, t.name, t.color FROM tags t JOIN submission_tags st ON t.tag_id = st.tag_id WHERE st.platform = 'mast' AND st.submission_id = ?",
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
        logger.error("Error in /api/mast/submissions/%s: %s", submission_id[:50], e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@mast_router.get("/submissions/{submission_id:path}/snapshots")
def get_mast_submission_snapshots(
    submission_id: str,
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
):
    conn = get_connection()
    try:
        # Resolve rkey if needed
        sub = mast_queries.get_mast_submission(conn, submission_id)
        if not sub:
            sub = mast_queries.get_mast_submission_by_rkey(conn, submission_id)
        full_id = sub["submission_id"] if sub else submission_id
        return {"snapshots": mast_queries.get_mast_snapshots(conn, full_id, start, end)}
    except Exception as e:
        logger.error("Error in /api/mast/submissions/%s/snapshots: %s", submission_id[:50], e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@mast_router.get("/aggregate")
def get_mast_aggregate(
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
    account_id: int | None = Query(None),
):
    conn = get_connection()
    try:
        return {"snapshots": mast_queries.get_mast_aggregate_snapshots(conn, start, end, account_id=account_id)}
    except Exception as e:
        logger.error("Error in /api/mast/aggregate: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@mast_router.get("/comparison")
def get_mast_comparison(
    ids: str = Query(..., description="Comma-separated status rkeys or URIs"),
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
):
    conn = get_connection()
    try:
        # Resolve rkeys to full URIs
        raw_ids = [x.strip() for x in ids.split(",") if x.strip()]
        if len(raw_ids) > 10:
            raise HTTPException(400, "Max 10 statuses for comparison")

        submission_ids = []
        titles = {}
        for rid in raw_ids:
            sub = mast_queries.get_mast_submission(conn, rid)
            if not sub:
                sub = mast_queries.get_mast_submission_by_rkey(conn, rid)
            if sub:
                full_id = sub["submission_id"]
                submission_ids.append(full_id)
                titles[full_id] = sub["title"]

        data = mast_queries.get_mast_comparison_snapshots(conn, submission_ids, start, end)
        return {"series": data, "titles": titles}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error in /api/mast/comparison: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@mast_router.get("/poll_log")
def get_mast_poll_log(limit: int = Query(50, ge=1, le=200)):
    conn = get_connection()
    try:
        return {"polls": mast_queries.get_mast_poll_log(conn, limit)}
    except Exception as e:
        logger.error("Error in /api/mast/poll_log: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


# -- MAST CSV Export ----------------------------------------------------------

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


@mast_router.get("/export/submissions")
def export_mast_submissions():
    conn = get_connection()
    try:
        subs = mast_queries.get_all_mast_submissions(conn)
        return _csv_response(subs, "mast_submissions.csv")
    finally:
        conn.close()


@mast_router.get("/export/snapshots")
def export_mast_snapshots(id: str | None = Query(None)):
    conn = get_connection()
    try:
        if id:
            # Resolve rkey if needed
            sub = mast_queries.get_mast_submission(conn, id)
            if not sub:
                sub = mast_queries.get_mast_submission_by_rkey(conn, id)
            full_id = sub["submission_id"] if sub else id
            snaps = mast_queries.get_mast_snapshots(conn, full_id)
        else:
            snaps = [dict(r) for r in conn.execute("SELECT * FROM mast_snapshots ORDER BY polled_at ASC").fetchall()]
        return _csv_response(snaps, f"mast_snapshots{'_' + id[:20] if id else ''}.csv")
    finally:
        conn.close()
