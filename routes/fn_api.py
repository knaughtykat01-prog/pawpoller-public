"""REST API endpoints for the FurryNetwork (fn) analytics dashboard.

OAuth2 (email+password → bearer/refresh). Poll+post gallery; work is grouped
under FN characters. Standard metric shape: views / favorites_count /
comments_count. Mirrors routes/e621_api.py.
"""
from __future__ import annotations

import csv
import io
import logging
from typing import Optional

from fastapi import APIRouter, Query, HTTPException
from fastapi.responses import StreamingResponse

from database.db import get_connection
from database import fn_queries
from polling.fn_poller import run_fn_poll_cycle, fn_poll_progress
from polling.background import spawn_poll
import config

logger = logging.getLogger(__name__)
fn_router = APIRouter(prefix="/api/fn")


# -- Auth --------------------------------------------------------------------

@fn_router.get("/auth/status")
def fn_auth_status():
    settings = config.get_settings()
    has_credentials = bool(settings.get("fn_username")
                           and (settings.get("fn_password") or settings.get("fn_refresh_token")))
    has_data = False
    conn = get_connection()
    try:
        has_data = conn.execute("SELECT COUNT(*) as c FROM fn_submissions").fetchone()["c"] > 0
    except Exception:
        pass
    finally:
        conn.close()
    return {"has_credentials": has_credentials, "has_data": has_data,
            "username": settings.get("fn_username", "")}


@fn_router.post("/auth/connect")
async def fn_connect(body: dict):
    """Connect FurryNetwork with a **refresh token**, or with email + password.

    The password grant was the only route until 2026-08-19, when FN put it
    behind reCAPTCHA: ``POST /api/oauth/token`` with ``grant_type=password`` now
    answers 422 ``{"message": "Invalid Recaptcha Token"}`` before it looks at the
    credentials at all. No headless client can satisfy that, so email+password
    can no longer work from here.

    The refresh grant is untouched — a bogus refresh token still comes back as a
    plain ``invalid_grant`` — so a token lifted from a real browser session
    works indefinitely, and every renewal writes the rotated one back.

    Email+password is still accepted rather than removed: it costs one branch,
    it is what will work again if FN drops the reCAPTCHA, and deleting it would
    make this route silently lossy for anyone whose install still authenticates.
    """
    email = str(body.get("username", "") or "").strip()
    password = str(body.get("password", "") or "").strip()
    refresh_token = str(body.get("refresh_token", "") or "").strip()

    if not refresh_token and not (email and password):
        raise HTTPException(
            400,
            "Paste a FurryNetwork refresh token (Settings → log in at furrynetwork.com, "
            "then copy the refresh_token from the site's local storage). Email and "
            "password no longer work: FurryNetwork put its login behind reCAPTCHA.")

    from clients.fn.client import FnClient, FnAuthError, FnRecaptchaError
    client = FnClient(username=email, password=password, refresh_token=refresh_token)
    try:
        await client.login()
        name = await client.validate_session()
    except FnRecaptchaError as e:
        # 400, not 401: nothing is wrong with the credentials and retrying them
        # will never help. The fix is a different kind of credential.
        raise HTTPException(400, str(e))
    except FnAuthError as e:
        raise HTTPException(401, str(e))
    except Exception as e:
        raise HTTPException(502, f"Failed to validate credentials: {e}")
    finally:
        await client.close()

    if not name:
        raise HTTPException(401, "Auth failed — FurryNetwork did not accept those credentials.")

    payload = {
        "fn_refresh_token": client.refresh_token,
        "fn_access_token": client.access_token,
        "fn_notifications_enabled": True,
    }
    # Only overwrite the stored login when one was actually supplied — a
    # token-only reconnect must not wipe an email that is still on file.
    if email:
        payload["fn_username"] = email
    if password:
        payload["fn_password"] = password
    if not email:
        # `fn_username` gates has_credentials and the session check, so a
        # token-only connect has to fill it or the account reads as unconnected.
        settings = config.get_settings()
        if not settings.get("fn_username"):
            payload["fn_username"] = name
    config.save_settings(payload)
    return {"status": "success", "message": f"Connected — tracking {name}"}


@fn_router.post("/auth/disconnect")
def fn_disconnect():
    config.delete_settings_keys(["fn_username", "fn_password", "fn_refresh_token", "fn_access_token"])
    config.save_settings({"fn_notifications_enabled": False})
    return {"status": "success", "message": "FurryNetwork disconnected"}


# -- Polling -----------------------------------------------------------------

@fn_router.get("/poll/progress")
def get_fn_poll_progress():
    return dict(fn_poll_progress)


@fn_router.post("/poll/trigger")
async def trigger_fn_poll():
    try:
        spawn_poll(run_fn_poll_cycle(), "run_fn_poll_cycle")
        return {"status": "started"}
    # Let an explicit HTTPException through — the ownership guard in
    # spawn_poll raises 409 here, and the blanket handler below would
    # otherwise report it as a 500 'internal error'.
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error in fn poll trigger: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))


@fn_router.post("/poll/full-resync")
async def fn_full_resync():
    try:
        spawn_poll(run_fn_poll_cycle(force_full=True), "run_fn_poll_cycle full-resync")
        return {"status": "started"}
    # Let an explicit HTTPException through — the ownership guard in
    # spawn_poll raises 409 here, and the blanket handler below would
    # otherwise report it as a 500 'internal error'.
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error in fn full resync: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))


# -- Data --------------------------------------------------------------------

@fn_router.get("/status")
def get_fn_status():
    conn = get_connection()
    try:
        return {
            "total_submissions": conn.execute("SELECT COUNT(*) as c FROM fn_submissions").fetchone()["c"],
            "total_snapshots": conn.execute("SELECT COUNT(*) as c FROM fn_snapshots").fetchone()["c"],
            "last_poll": fn_queries.get_fn_last_poll(conn),
        }
    except Exception as e:
        logger.error("Error in /api/fn/status: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@fn_router.get("/summary")
def get_fn_summary(account_id: int | None = Query(None)):
    conn = get_connection()
    try:
        summary = fn_queries.get_fn_summary(conn, account_id=account_id)
        summary["growth_rates"] = fn_queries.get_fn_growth_rates(conn)
        return summary
    except Exception as e:
        logger.error("Error in /api/fn/summary: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@fn_router.get("/submissions")
def get_fn_submissions(
    sort_by: str = Query("views"),
    order: str = Query("desc"),
    search: str = Query(""),
    account_id: int | None = Query(None),
):
    conn = get_connection()
    try:
        subs = fn_queries.get_all_fn_submissions(conn, sort_by=sort_by, order=order, account_id=account_id)
        deltas = fn_queries.get_fn_submission_deltas(conn)
        if search:
            sl = search.lower()
            subs = [s for s in subs if sl in s["title"].lower() or sl in (s.get("keywords") or "").lower()]
        for s in subs:
            d = deltas.get(s["submission_id"], {})
            s["views_delta"] = d.get("views_delta", 0)
            s["favorites_delta"] = d.get("favorites_delta", 0)
            s["comments_delta"] = d.get("comments_delta", 0)
        return {"submissions": subs, "total": len(subs)}
    except Exception as e:
        logger.error("Error in /api/fn/submissions: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@fn_router.get("/submissions/{submission_id:path}")
def get_fn_submission(submission_id: str):
    conn = get_connection()
    try:
        sub = fn_queries.get_fn_submission(conn, submission_id)
        if not sub:
            raise HTTPException(status_code=404, detail="FurryNetwork submission not found")
        full_id = sub["submission_id"]
        try:
            tags = conn.execute(
                "SELECT t.tag_id, t.name, t.color FROM tags t JOIN submission_tags st "
                "ON t.tag_id = st.tag_id WHERE st.platform = 'fn' AND st.submission_id = ?",
                (full_id,)).fetchall()
        except Exception:
            tags = []
        sub_dict = dict(sub)
        sub_dict["tags"] = [dict(r) for r in tags]
        return {
            "submission": sub_dict,
            "snapshots": fn_queries.get_fn_snapshots(conn, full_id),
            "growth_rates": fn_queries.get_fn_submission_growth_rates(conn, full_id),
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error in /api/fn/submissions/%s: %s", submission_id[:50], e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@fn_router.get("/submissions/{submission_id:path}/snapshots")
def get_fn_submission_snapshots(submission_id: str, start: Optional[str] = Query(None),
                                end: Optional[str] = Query(None)):
    conn = get_connection()
    try:
        return {"snapshots": fn_queries.get_fn_snapshots(conn, submission_id, start, end)}
    except Exception as e:
        logger.error("Error in /api/fn/.../snapshots: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@fn_router.get("/aggregate")
def get_fn_aggregate(start: Optional[str] = Query(None), end: Optional[str] = Query(None),
                     account_id: int | None = Query(None)):
    conn = get_connection()
    try:
        return {"snapshots": fn_queries.get_fn_aggregate_snapshots(conn, start, end, account_id=account_id)}
    except Exception as e:
        logger.error("Error in /api/fn/aggregate: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@fn_router.get("/comparison")
def get_fn_comparison(ids: str = Query(...), start: Optional[str] = Query(None),
                      end: Optional[str] = Query(None)):
    conn = get_connection()
    try:
        raw_ids = [x.strip() for x in ids.split(",") if x.strip()]
        if len(raw_ids) > 10:
            raise HTTPException(400, "Max 10 submissions for comparison")
        submission_ids, titles = [], {}
        for rid in raw_ids:
            sub = fn_queries.get_fn_submission(conn, rid)
            if sub:
                submission_ids.append(sub["submission_id"])
                titles[sub["submission_id"]] = sub["title"]
        data = fn_queries.get_fn_comparison_snapshots(conn, submission_ids, start, end)
        return {"series": data, "titles": titles}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error in /api/fn/comparison: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@fn_router.get("/poll_log")
def get_fn_poll_log(limit: int = Query(50, ge=1, le=200)):
    conn = get_connection()
    try:
        return {"polls": fn_queries.get_fn_poll_log(conn, limit)}
    except Exception as e:
        logger.error("Error in /api/fn/poll_log: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


# -- CSV Export --------------------------------------------------------------

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


@fn_router.get("/export/submissions")
def export_fn_submissions():
    conn = get_connection()
    try:
        return _csv_response(fn_queries.get_all_fn_submissions(conn), "fn_submissions.csv")
    finally:
        conn.close()


@fn_router.get("/export/snapshots")
def export_fn_snapshots(id: str | None = Query(None)):
    conn = get_connection()
    try:
        if id:
            snaps = fn_queries.get_fn_snapshots(conn, id)
        else:
            snaps = [dict(r) for r in conn.execute(
                "SELECT * FROM fn_snapshots ORDER BY polled_at ASC").fetchall()]
        return _csv_response(snaps, f"fn_snapshots{'_' + id[:20] if id else ''}.csv")
    finally:
        conn.close()
