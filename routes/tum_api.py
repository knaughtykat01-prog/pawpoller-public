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

from fastapi import APIRouter, Query, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse

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


# Registered BEFORE the bare /submissions/{id} route below. The `:path`
# converter is greedy and Starlette matches in registration order, so with
# the bare route first this URL resolved to a submission id of
# "<id>/snapshots" and returned 404 for every post that exists.
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


# -- TUM posting sign-in: the Connect button (spec 024, 4.60.0) -----------------
#
# Tumblr OAuth 2, authorisation-code grant, the SoundCloud flow's shape (routes/sc_api.py): connect →
# Tumblr's approval page → /auth/callback, which exchanges the code, asks Tumblr who approved it and keeps
# the tokens only if that user owns the account's blog (the DeviantArt 3.32.2 "who approved it" guard).
# The app key + secret are the existing `tum_api_key` / `tum_consumer_secret`; polling is unchanged.

TUM_POST_FIELDS = ("tum_oauth2_access_token", "tum_oauth2_refresh_token", "tum_oauth2_expires_at", "tum_oauth2_user")
_tum_oauth_state: dict[str, dict] = {}
_TUM_STATE_TTL = 900


def _tum_account(account_id) -> tuple[int | None, bool]:
    if not account_id:
        return None, True
    from database import accounts as adb
    conn = get_connection()
    try:
        acct = adb.get_account(conn, int(account_id))
    finally:
        conn.close()
    if not acct or acct["platform"] != "tum":
        raise HTTPException(404, f"No Tumblr account {account_id}")
    return int(account_id), bool(acct["is_default"])


def _tum_creds(aid: int | None, is_default: bool) -> dict:
    settings = config.get_settings()
    if aid is None:
        return {k: settings.get(k, "") for k in config.PLATFORM_CREDENTIAL_FIELDS["tum"]}
    return config.resolve_account_credentials("tum", aid, is_default, settings)


def _tum_key(field: str, aid: int | None, is_default: bool) -> str:
    return field if aid is None else config.account_setting_key(aid, field, is_default)


def _tum_redirect_uri(request: Request) -> str:
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    return f"{proto}://{host}/api/tum/auth/callback"


@tum_router.get("/auth/posting/status")
def tum_posting_status(account_id: int | None = Query(None)):
    aid, is_default = _tum_account(account_id)
    c = _tum_creds(aid, is_default)
    oauth2 = bool(c.get("tum_oauth2_refresh_token") or c.get("tum_oauth2_access_token"))
    oauth1 = bool(c.get("tum_consumer_secret") and c.get("tum_oauth_token") and c.get("tum_oauth_token_secret"))
    return {"connected": oauth2 or oauth1, "method": "oauth2" if oauth2 else "oauth1" if oauth1 else "none",
            "user": c.get("tum_oauth2_user", "") or "", "blog": c.get("tum_blog", "") or "",
            "has_app": bool(c.get("tum_api_key") and c.get("tum_consumer_secret")),
            "can_post": bool(c.get("tum_blog")) and (oauth2 or oauth1)}


@tum_router.post("/auth/posting/connect")
def tum_posting_connect(body: dict, request: Request):
    """Save the app key + secret when given, then hand back Tumblr's approval URL and the return address
    to register in the Tumblr app."""
    aid, is_default = _tum_account(body.get("account_id"))
    c = _tum_creds(aid, is_default)
    key = str(body.get("client_id") or "").strip() or c.get("tum_api_key", "")
    secret = str(body.get("client_secret") or "").strip() or c.get("tum_consumer_secret", "")
    if not key or not secret:
        raise HTTPException(400, "Paste your Tumblr app's OAuth Consumer Key and Secret first "
                                 "(tumblr.com/oauth/apps)")
    if not c.get("tum_blog"):
        raise HTTPException(400, "Add the blog this account posts to first (the same blog PawPoller tracks)")
    config.save_settings({_tum_key("tum_api_key", aid, is_default): key,
                          _tum_key("tum_consumer_secret", aid, is_default): secret})
    import secrets
    import time
    from clients.tum.writer import authorize_url
    now = time.time()
    for old in [s for s, v in _tum_oauth_state.items() if now - v.get("at", 0) > _TUM_STATE_TTL]:
        _tum_oauth_state.pop(old, None)
    state = secrets.token_urlsafe(24)
    _tum_oauth_state[state] = {"at": now, "account_id": aid, "is_default": is_default}
    redirect_uri = _tum_redirect_uri(request)
    return {"url": authorize_url(key, redirect_uri, state), "redirect_uri": redirect_uri}


def _tum_page(title: str, detail: str, ok: bool) -> HTMLResponse:
    from html import escape
    colour = "#3fb950" if ok else "#f85149"
    return HTMLResponse(
        f"<html><head><title>{escape(title)}</title></head>"
        f"<body style='font-family:system-ui,sans-serif;background:#0d1117;color:#c9d1d9;padding:48px;"
        f"line-height:1.6'><h2 style='color:{colour}'>{escape(title)}</h2><p>{escape(detail)}</p>"
        f"<p style='color:#8b949e;font-size:13px'>You can close this tab.</p></body></html>",
        status_code=200 if ok else 400)


@tum_router.get("/auth/callback")
async def tum_auth_callback(request: Request, code: str = "", state: str = "", error: str = "",
                            error_description: str = ""):
    if error:
        return _tum_page("Tumblr said no", f"Tumblr said: {error_description or error}", False)
    if not code or state not in _tum_oauth_state:
        _tum_oauth_state.pop(state, None)
        return _tum_page("That sign-in link has expired", "Press Connect again in PawPoller.", False)
    entry = _tum_oauth_state.pop(state)             # single use, whatever happens next
    aid, is_default = entry.get("account_id"), bool(entry.get("is_default", True))
    c = _tum_creds(aid, is_default)
    blog = c.get("tum_blog", "")
    from clients.tum import writer as w
    try:
        tokens = await w.exchange_code(code, c.get("tum_api_key", ""), c.get("tum_consumer_secret", ""),
                                       _tum_redirect_uri(request))
        async with w.TumWriter(blog, access_token=tokens["tum_oauth2_access_token"]) as tw:
            who = await tw.user_info()
    except w.TumError as e:
        return _tum_page("Couldn't connect Tumblr", str(e), False)
    from posting.platforms.tumblr import owns
    if not owns(who, blog):
        logger.warning("TUM: refused a sign-in whose user doesn't own the account's blog (account=%s)", aid)
        return _tum_page("Wrong Tumblr account",
                         f"{who['name'] or 'That Tumblr user'} doesn't own {blog}. Nothing was saved. Tumblr signs in "
                         f"whoever the browser is logged in as: log in as the blog's owner and press Connect again.",
                         False)
    tokens["tum_oauth2_user"] = who["name"]
    config.save_settings({_tum_key(k, aid, is_default): v for k, v in tokens.items()})
    logger.info("TUM: posting sign-in stored (account=%s)", aid)
    return _tum_page("Tumblr connected", f"Connected as {who['name']} · posts to {blog}. PawPoller renews the "
                                         f"sign-in by itself from here.", True)


@tum_router.post("/auth/posting/disconnect")
def tum_posting_disconnect(body: dict | None = None):
    """Forget the Connect sign-in only; the app key, blog and any pasted OAuth 1 keys stay."""
    aid, is_default = _tum_account((body or {}).get("account_id"))
    config.delete_settings_keys([_tum_key(k, aid, is_default) for k in TUM_POST_FIELDS])
    return {"status": "success"}
