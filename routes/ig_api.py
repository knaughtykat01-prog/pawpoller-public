"""REST API endpoints for the Instagram (IG) analytics dashboard.

Official Instagram Graph API (graph.instagram.com — the "Instagram API with
Instagram Login" flow). OAuth long-lived access token + optional target user_id.
Tracks views, reach, likes, comments, saved, shares. Post IDs are numeric media ids.
"""

from __future__ import annotations
import csv
import io
import logging
from typing import Optional

import time

from fastapi import APIRouter, Query, HTTPException, UploadFile, File, Request
from fastapi.responses import StreamingResponse, FileResponse

from database.db import get_connection
from database import ig_queries
from polling.ig_poller import run_ig_poll_cycle, ig_poll_progress
from polling.background import spawn_poll
import config

logger = logging.getLogger(__name__)
ig_router = APIRouter(prefix="/api/ig")


# -- IG Auth ------------------------------------------------------------------


@ig_router.get("/auth/status")
def ig_auth_status():
    settings = config.get_settings()
    has_credentials = bool(settings.get("ig_access_token"))
    has_data = False
    conn = get_connection()
    try:
        count = conn.execute("SELECT COUNT(*) as c FROM ig_submissions").fetchone()["c"]
        has_data = count > 0
    except Exception:
        pass
    finally:
        conn.close()
    return {
        "has_credentials": has_credentials,
        "has_data": has_data,
        "username": settings.get("ig_username", "") or settings.get("ig_user_id", ""),
    }


@ig_router.post("/auth/connect")
async def ig_connect(body: dict):
    """Validate an Instagram access token (and optional user_id) and save it."""
    access_token = body.get("access_token", "").strip()
    user_id = str(body.get("user_id", "") or "").strip()

    if not access_token:
        raise HTTPException(400, "Access token is required (long-lived Instagram user token from a Meta app with instagram_business_basic + instagram_business_manage_insights, for a Business/Creator account)")

    from polling.ig_poller import _get_or_create_client
    overlay = {
        **config.get_settings(),
        "ig_access_token": access_token,
        "ig_user_id": user_id,
    }
    client = _get_or_create_client(overlay, access_token, user_id)
    try:
        name = await client.validate_session()
    except Exception as e:
        raise HTTPException(502, f"Failed to validate credentials: {e}")

    if not name:
        raise HTTPException(401, "Auth failed — the access token is invalid or lacks the instagram_business_basic / instagram_business_manage_insights scopes, or the account is not a Business/Creator account.")

    config.save_settings({
        # the client may have refreshed (rotated) the long-lived token
        "ig_access_token": client.access_token,
        "ig_user_id": client.user_id,
        "ig_username": name,
        "ig_notifications_enabled": True,
    })

    note = ""
    if client.ignored_user_id:
        # Never silently do something other than what was asked for.
        note = (" — the User ID box held a handle, not a numeric id, so the id "
                "was read from your access token instead")
    return {"status": "success", "message": f"Connected — tracking {name}{note}"}


@ig_router.post("/auth/disconnect")
def ig_disconnect():
    config.delete_settings_keys(["ig_access_token", "ig_user_id", "ig_username"])
    config.save_settings({"ig_notifications_enabled": False})
    return {"status": "success", "message": "Instagram disconnected"}


# -- IG Public post-image hosting (for the Posts module) ----------------------
# Instagram's Content Publishing API makes Meta cURL a public image_url, so a
# to-be-posted image is stashed and served here, unauthenticated, for the few
# seconds of an active publish. This path is auth-exempt (dashboard.py) and the
# token is an unguessable uuid4 hex with a strict format check + short TTL.


@ig_router.get("/pubmedia/{token}")
def ig_pubmedia(token: str):
    from posting import ig_media
    p = ig_media.path_for(token)
    if not p:
        raise HTTPException(404, "Not found")
    return FileResponse(str(p), media_type="image/jpeg")


@ig_router.post("/pubmedia")
async def ig_stash_pubmedia(file: UploadFile = File(...)):
    """Stash an uploaded image and return the public URL Meta will fetch.

    Authenticated (unlike the GET above, which must be open for Meta). This lets a
    **paired desktop** instance — which has no public address of its own — borrow
    this public server as the image host for an Instagram post: the desktop uploads
    the image here, gets back a public URL on this server, then creates the IG
    container itself. Auth is the same Bearer API key the desktop already uses for
    story/artwork sync, so no extra credentials are needed.
    """
    base = config.get_settings().get("ig_public_base_url", "").strip()
    if not base:
        raise HTTPException(503, "This server has no IG_PUBLIC_BASE_URL configured, so it can't host Instagram images.")
    data = await file.read()
    if not data:
        raise HTTPException(400, "Empty image upload")
    if len(data) > 12 * 1024 * 1024:
        raise HTTPException(413, "Image too large (max 12 MB)")
    from posting import ig_media
    try:
        token = ig_media.stash_bytes(data)
    except Exception as e:
        raise HTTPException(400, f"Could not process image: {e}")
    return {"token": token, "url": ig_media.public_url(base, token)}


# -- The open relay + the desktop's image-host ladder (4.7.0) -----------------
# A desktop has no public address for Meta to fetch from. Any PawPoller server
# with `ig_relay_open` on hosts an image for anyone, no key, for the stash TTL:
# the same stash + public GET as the paired route above, behind a size cap, an
# image-type check (PIL re-encodes to JPEG, so nothing but pixels is served), a
# per-address rate limit and a global pending cap. Auth-exempt in dashboard.py.

_RELAY_HITS: dict[str, list[float]] = {}
_IMAGE_MAGIC = (b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n", b"RIFF", b"GIF8")


def _client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "?"


def _relay_rate_ok(ip: str, now: float | None = None) -> bool:
    limit, window = config.IG_RELAY_PER_IP
    now = time.time() if now is None else now
    hits = [t for t in _RELAY_HITS.get(ip, []) if now - t < window]
    if len(hits) >= limit:
        _RELAY_HITS[ip] = hits
        return False
    hits.append(now)
    _RELAY_HITS[ip] = hits
    if len(_RELAY_HITS) > 5000:          # never let the table itself grow unbounded
        for k in [k for k, v in _RELAY_HITS.items() if not v or now - v[-1] > window]:
            _RELAY_HITS.pop(k, None)
    return True


async def _read_capped(file: UploadFile, cap: int) -> bytes:
    chunks, total = [], 0
    while True:
        chunk = await file.read(1 << 16)
        if not chunk:
            break
        total += len(chunk)
        if total > cap:
            raise HTTPException(413, f"Image too large (max {cap // (1024 * 1024)} MB)")
        chunks.append(chunk)
    return b"".join(chunks)


@ig_router.post("/relay")
async def ig_relay(request: Request, file: UploadFile = File(...)):
    """Host one image for an install that has no public address (no auth).

    Returns the public URL Meta will fetch; the image self-expires on the stash
    TTL. Refuses when this server has not opted in (`ig_relay_open`), has no
    public base, is rate-limited for this address, or is already hosting the
    cap. The body must decode as an image — PIL re-encodes it to JPEG, so the
    URL can only ever serve pixels.
    """
    s = config.get_settings()
    if not s.get("ig_relay_open", False):
        raise HTTPException(403, "This PawPoller server does not host Instagram images for other installs.")
    base = s.get("ig_public_base_url", "").strip()
    if not base:
        raise HTTPException(503, "This server has no IG_PUBLIC_BASE_URL configured, so it can't host Instagram images.")
    ip = _client_ip(request)
    if not _relay_rate_ok(ip):
        raise HTTPException(429, "Too many images from this address — try again in a few minutes.")
    from posting import ig_media
    if ig_media.pending_count() >= config.IG_RELAY_MAX_PENDING:
        raise HTTPException(503, "The relay is busy hosting other images right now — try again in a few minutes.")
    data = await _read_capped(file, config.IG_RELAY_MAX_BYTES)
    if not data:
        raise HTTPException(400, "Empty image upload")
    if not data.startswith(_IMAGE_MAGIC):
        raise HTTPException(400, "Not an image")
    try:
        token = ig_media.stash_bytes(data)
    except Exception as e:
        raise HTTPException(400, f"Could not process image: {e}")
    logger.info("IG relay: hosting one image (%d KB) for a remote install", len(data) // 1024)
    return {"url": ig_media.public_url(base, token), "expires_in": ig_media.TTL_SECONDS}


def _host_status() -> dict:
    from posting import ig_tunnel
    from posting.scheduler import detect_runtime_mode
    from posting.ig_host import relay_url
    s = config.get_settings()

    def on(key: str) -> bool:
        v = s.get(key, True)
        return v.strip().lower() not in ("", "0", "false", "no", "off") if isinstance(v, str) else bool(v)
    return {
        "runtime": detect_runtime_mode(),
        "local_base": s.get("ig_public_base_url", "").strip(),
        "paired": bool((s.get("posting_server_url") or "").strip()),
        "relay": {"enabled": on("ig_relay_enabled"), "url": relay_url(s),
                  "default_url": config.IG_RELAY_DEFAULT_URL, "open": bool(s.get("ig_relay_open", False))},
        "tunnel": {"enabled": on("ig_tunnel_enabled"), **ig_tunnel.helper_status()},
    }


@ig_router.get("/host-status")
def ig_host_status():
    """The image-host ladder as this instance sees it — for Settings → Posting."""
    return _host_status()


@ig_router.post("/host-settings")
def ig_host_settings(body: dict):
    upd: dict = {}
    for key in ("ig_relay_enabled", "ig_tunnel_enabled", "ig_relay_open"):
        if key in body:
            upd[key] = bool(body[key])
    if "ig_relay_url" in body:
        url = str(body.get("ig_relay_url") or "").strip()
        if url and not url.startswith(("https://", "http://")):
            raise HTTPException(400, "The relay URL must start with https://")
        upd["ig_relay_url"] = url
    if not upd:
        raise HTTPException(400, "Nothing to save")
    config.save_settings(upd)
    return _host_status()


@ig_router.post("/tunnel-helper/download")
async def ig_tunnel_helper_download():
    from posting import ig_tunnel
    try:
        return await ig_tunnel.download_helper()
    except Exception as e:
        raise HTTPException(502, str(e))


@ig_router.post("/tunnel-helper/remove")
def ig_tunnel_helper_remove():
    from posting import ig_tunnel
    return ig_tunnel.remove_helper()


@ig_router.post("/tunnel-helper/test")
async def ig_tunnel_helper_test():
    """Open a tunnel, prove Cloudflare answers through it, close it."""
    from posting import ig_tunnel
    try:
        host = await ig_tunnel.open_public_host()
    except Exception as e:
        raise HTTPException(502, str(e))
    url = host.base_url
    await host.close()
    return {"ok": True, "url": url}


# -- IG Polling ---------------------------------------------------------------


@ig_router.get("/poll/progress")
def get_ig_poll_progress():
    return dict(ig_poll_progress)


@ig_router.post("/poll/trigger")
async def trigger_ig_poll():
    try:
        spawn_poll(run_ig_poll_cycle(), "run_ig_poll_cycle")
        return {"status": "started"}
    # Let an explicit HTTPException through — the ownership guard in
    # spawn_poll raises 409 here, and the blanket handler below would
    # otherwise report it as a 500 'internal error'.
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error in IG poll trigger: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))


@ig_router.post("/poll/full-resync")
async def ig_full_resync():
    try:
        spawn_poll(run_ig_poll_cycle(force_full=True), "run_ig_poll_cycle full-resync")
        return {"status": "started"}
    # Let an explicit HTTPException through — the ownership guard in
    # spawn_poll raises 409 here, and the blanket handler below would
    # otherwise report it as a 500 'internal error'.
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error in IG full resync: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))


# -- IG Data ------------------------------------------------------------------


@ig_router.get("/status")
def get_ig_status():
    conn = get_connection()
    try:
        last_poll = ig_queries.get_ig_last_poll(conn)
        count = conn.execute("SELECT COUNT(*) as c FROM ig_submissions").fetchone()["c"]
        snap_count = conn.execute("SELECT COUNT(*) as c FROM ig_snapshots").fetchone()["c"]
        return {
            "total_submissions": count,
            "total_snapshots": snap_count,
            "last_poll": last_poll,
        }
    except Exception as e:
        logger.error("Error in /api/ig/status: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@ig_router.get("/summary")
def get_ig_summary(account_id: int | None = Query(None)):
    conn = get_connection()
    try:
        summary = ig_queries.get_ig_summary(conn, account_id=account_id)
        summary["growth_rates"] = ig_queries.get_ig_growth_rates(conn)
        return summary
    except Exception as e:
        logger.error("Error in /api/ig/summary: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@ig_router.get("/submissions")
def get_ig_submissions(
    sort_by: str = Query("views", description="Sort field"),
    order: str = Query("desc", description="Sort order"),
    search: str = Query("", description="Search title/keywords"),
    account_id: int | None = Query(None),
):
    conn = get_connection()
    try:
        subs = ig_queries.get_all_ig_submissions(conn, sort_by=sort_by, order=order, account_id=account_id)
        deltas = ig_queries.get_ig_submission_deltas(conn)

        if search:
            search_lower = search.lower()
            subs = [s for s in subs if search_lower in s["title"].lower() or search_lower in (s.get("keywords") or "").lower()]

        for s in subs:
            d = deltas.get(s["submission_id"], {})
            s["views_delta"] = d.get("views_delta", 0)
            s["reach_delta"] = d.get("reach_delta", 0)
            s["likes_delta"] = d.get("likes_delta", 0)
            s["comments_delta"] = d.get("comments_delta", 0)
            s["saved_delta"] = d.get("saved_delta", 0)
            s["shares_delta"] = d.get("shares_delta", 0)

        return {"submissions": subs, "total": len(subs)}
    except Exception as e:
        logger.error("Error in /api/ig/submissions: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


# Registered BEFORE the bare /submissions/{id} route below. The `:path`
# converter is greedy and Starlette matches in registration order, so with
# the bare route first this URL resolved to a submission id of
# "<id>/snapshots" and returned 404 for every post that exists.
@ig_router.get("/submissions/{submission_id:path}/snapshots")
def get_ig_submission_snapshots(
    submission_id: str,
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
):
    conn = get_connection()
    try:
        return {"snapshots": ig_queries.get_ig_snapshots(conn, submission_id, start, end)}
    except Exception as e:
        logger.error("Error in /api/ig/submissions/%s/snapshots: %s", submission_id[:50], e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@ig_router.get("/submissions/{submission_id:path}")
def get_ig_submission(submission_id: str):
    conn = get_connection()
    try:
        sub = ig_queries.get_ig_submission(conn, submission_id)
        if not sub:
            raise HTTPException(status_code=404, detail="Instagram post not found")

        full_id = sub["submission_id"]
        snapshots = ig_queries.get_ig_snapshots(conn, full_id)
        growth_rates = ig_queries.get_ig_submission_growth_rates(conn, full_id)
        try:
            tags = conn.execute(
                "SELECT t.tag_id, t.name, t.color FROM tags t JOIN submission_tags st ON t.tag_id = st.tag_id WHERE st.platform = 'ig' AND st.submission_id = ?",
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
        logger.error("Error in /api/ig/submissions/%s: %s", submission_id[:50], e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@ig_router.get("/aggregate")
def get_ig_aggregate(
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
    account_id: int | None = Query(None),
):
    conn = get_connection()
    try:
        return {"snapshots": ig_queries.get_ig_aggregate_snapshots(conn, start, end, account_id=account_id)}
    except Exception as e:
        logger.error("Error in /api/ig/aggregate: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@ig_router.get("/comparison")
def get_ig_comparison(
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
            sub = ig_queries.get_ig_submission(conn, rid)
            if sub:
                submission_ids.append(sub["submission_id"])
                titles[sub["submission_id"]] = sub["title"]

        data = ig_queries.get_ig_comparison_snapshots(conn, submission_ids, start, end)
        return {"series": data, "titles": titles}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error in /api/ig/comparison: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@ig_router.get("/poll_log")
def get_ig_poll_log(limit: int = Query(50, ge=1, le=200)):
    conn = get_connection()
    try:
        return {"polls": ig_queries.get_ig_poll_log(conn, limit)}
    except Exception as e:
        logger.error("Error in /api/ig/poll_log: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


# -- IG CSV Export ------------------------------------------------------------

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


@ig_router.get("/export/submissions")
def export_ig_submissions():
    conn = get_connection()
    try:
        subs = ig_queries.get_all_ig_submissions(conn)
        return _csv_response(subs, "ig_submissions.csv")
    finally:
        conn.close()


@ig_router.get("/export/snapshots")
def export_ig_snapshots(id: str | None = Query(None)):
    conn = get_connection()
    try:
        if id:
            snaps = ig_queries.get_ig_snapshots(conn, id)
        else:
            snaps = [dict(r) for r in conn.execute("SELECT * FROM ig_snapshots ORDER BY polled_at ASC").fetchall()]
        return _csv_response(snaps, f"ig_snapshots{'_' + id[:20] if id else ''}.csv")
    finally:
        conn.close()
