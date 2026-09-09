"""REST API endpoints for YouTube (yt) — MEDIAPLATS §6 (4.24.0).

Google OAuth 2.0 + PKCE, one authorisation for both halves (a user token reads the channel's
stats and uploads). The flow is the sc routes' (routes/sc_api.py) with Google's endpoints and
the offline-access consent: ``POST /auth/connect`` saves the project's client id + secret and
returns the authorize URL (PKCE verifier server-side, single-use state); ``GET /auth/callback``
exchanges the code, asks ``channels.list?mine=true`` whose channel approved it, refuses a
mismatch against the handle the account is configured as, stores the pair + handle;
``/auth/status`` also says whether the channel is verified for long uploads.

The connect panel's honesty: every upload from an unverified Google project stays private
until YouTube's API compliance audit; ``/auth/status`` carries that sentence.
"""
from __future__ import annotations

import csv
import io
import logging
import secrets
import time
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, StreamingResponse

import config
from database import yt_queries
from database.db import get_connection
from polling.background import spawn_poll
from polling.yt_poller import run_yt_poll_cycle, yt_poll_progress

logger = logging.getLogger(__name__)
yt_router = APIRouter(prefix="/api/yt")

APP_FIELDS = ("yt_client_id", "yt_client_secret")
TOKEN_FIELDS = ("yt_access_token", "yt_refresh_token", "yt_token_expires_at", "yt_username", "yt_long_uploads")

# state → {"at", "verifier", "account_id", "is_default"}; in-process and single-use,
# exactly as the DA flow keeps it (a desktop app and a one-box server).
_yt_oauth_state: dict[str, dict] = {}
_SC_STATE_TTL = 900


def _account(account_id: int | None) -> tuple[int | None, bool]:
    """(account_id, is_default) — None means the platform default's bare keys."""
    if not account_id:
        return None, True
    from database import accounts as adb
    conn = get_connection()
    try:
        acct = adb.get_account(conn, int(account_id))
    finally:
        conn.close()
    if not acct:
        raise HTTPException(404, f"No account {account_id}")
    return int(account_id), bool(acct["is_default"])


def _creds(account_id: int | None) -> dict:
    settings = config.get_settings()
    aid, is_default = _account(account_id)
    if aid is None:
        return {k: settings.get(k, "") for k in APP_FIELDS + TOKEN_FIELDS}
    return config.resolve_account_credentials("yt", aid, is_default, settings)


def _key(field: str, account_id: int | None, is_default: bool) -> str:
    return field if account_id is None else config.account_setting_key(account_id, field, is_default)


def _redirect_uri(request: Request) -> str:
    """The callback as the browser sees it (forwarded headers behind Caddy + Cloudflare)."""
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    return f"{proto}://{host}/api/yt/auth/callback"


# ── Auth ─────────────────────────────────────────────────────────────────────

@yt_router.get("/auth/status")
def sc_auth_status(account_id: int | None = Query(None)):
    creds = _creds(account_id)
    has_app = bool(creds.get("yt_client_id") and creds.get("yt_client_secret"))
    has_token = bool(creds.get("yt_refresh_token"))
    has_data = False
    conn = get_connection()
    try:
        has_data = conn.execute("SELECT COUNT(*) as c FROM yt_submissions").fetchone()["c"] > 0
    except Exception:
        pass
    finally:
        conn.close()
    return {"has_app": has_app, "has_credentials": has_app and has_token, "has_token": has_token,
            "has_data": has_data, "username": creds.get("yt_username", "") or "",
            "long_uploads": creds.get("yt_long_uploads", "") or "",
            "audit_note": "Uploads from an unverified Google project stay private until YouTube's API "
                          "compliance audit passes; a Testing-mode consent screen expires its tokens every 7 days."}


@yt_router.post("/auth/connect")
def sc_connect(body: dict, request: Request):
    """Save the app credentials and hand back the authorize URL (PKCE state kept here)."""
    client_id = str(body.get("client_id", "") or "").strip()
    client_secret = str(body.get("client_secret", "") or "").strip()
    account_id = body.get("account_id")
    aid, is_default = _account(int(account_id) if account_id else None)
    existing = _creds(aid)
    client_id = client_id or existing.get("yt_client_id", "")
    client_secret = client_secret or existing.get("yt_client_secret", "")
    if not client_id or not client_secret:
        raise HTTPException(400, "The YouTube app's client id and client secret are both required")
    config.save_settings({_key("yt_client_id", aid, is_default): client_id,
                          _key("yt_client_secret", aid, is_default): client_secret})
    return _authorize(request, aid, is_default, client_id)


@yt_router.get("/auth/authorize-url")
def sc_authorize_url(request: Request, account_id: int | None = Query(None)):
    aid, is_default = _account(account_id)
    creds = _creds(aid)
    if not (creds.get("yt_client_id") and creds.get("yt_client_secret")):
        raise HTTPException(400, "Save the YouTube app's client id and secret first")
    return _authorize(request, aid, is_default, creds["yt_client_id"])


def _authorize(request: Request, aid: int | None, is_default: bool, client_id: str) -> dict:
    from clients.yt.client import authorize_url, pkce_pair
    now = time.time()
    for old in [s for s, v in _yt_oauth_state.items() if now - v.get("at", 0) > _SC_STATE_TTL]:
        _yt_oauth_state.pop(old, None)
    state = secrets.token_urlsafe(24)
    verifier, challenge = pkce_pair()
    _yt_oauth_state[state] = {"at": now, "verifier": verifier, "account_id": aid, "is_default": is_default}
    redirect_uri = _redirect_uri(request)
    return {"url": authorize_url(client_id, redirect_uri, challenge, state), "redirect_uri": redirect_uri,
            "state": state}


def _page(title: str, detail: str, ok: bool) -> HTMLResponse:
    """Escaped: `detail` carries the query's error_description, a remote body, exception text."""
    from html import escape
    colour = "#3fb950" if ok else "#f85149"
    title_s, detail_s = escape(title), escape(detail)
    return HTMLResponse(
        f"<html><head><title>{title_s}</title></head>"
        f"<body style='font-family:system-ui,sans-serif;background:#0d1117;color:#c9d1d9;"
        f"padding:48px;line-height:1.6'>"
        f"<h2 style='color:{colour}'>{title_s}</h2><p>{detail_s}</p>"
        f"<p style='color:#8b949e;font-size:13px'>You can close this tab.</p></body></html>",
        status_code=200 if ok else 400)


@yt_router.get("/auth/callback")
async def sc_auth_callback(request: Request, code: str = "", state: str = "",
                           error: str = "", error_description: str = ""):
    if error:
        return _page("Authorisation refused", f"YouTube said: {error_description or error}", False)
    if not code:
        return _page("Authorisation failed", "YouTube returned no code.", False)
    if state not in _yt_oauth_state:
        return _page("Authorisation failed", "That approval did not come from this install, or it expired. "
                                             "Start again from Settings.", False)
    entry = _yt_oauth_state.pop(state, {})          # single-use, popped whatever happens next
    aid, is_default = entry.get("account_id"), bool(entry.get("is_default", True))
    creds = _creds(aid)
    if not (creds.get("yt_client_id") and creds.get("yt_client_secret")):
        return _page("Authorisation failed", "The YouTube app credentials are no longer in settings.", False)

    from clients.yt.client import YtAuthError, YtClient
    client = YtClient(client_id=creds["yt_client_id"], client_secret=creds["yt_client_secret"])
    try:
        await client.exchange_code(code, _redirect_uri(request), entry.get("verifier", ""))
        approved_by = await client.validate_session() or ""
    except YtAuthError as e:
        await client.close()
        return _page("Authorisation failed", str(e), False)
    except Exception as e:
        await client.close()
        return _page("Authorisation failed", f"Could not reach Google: {e}", False)
    if not client.refresh_token:
        return _page("Authorisation failed", "YouTube issued no refresh token.", False)

    # Which YouTube account approved this? YouTube authorises whoever the browser is
    # signed in as, not the account whose button was pressed (the DA 3.32.2 trap).
    expected = (creds.get("yt_username") or "").strip()
    if approved_by and expected and approved_by.strip().lower() != expected.lower():
        logger.warning("YT: refused an authorisation approved by %s for the account configured as %s",
                       approved_by, expected)
        return _page("Wrong YouTube channel",
                     f"That approval came from {approved_by}, but this account is {expected}. Nothing was "
                     f"saved. Google authorises whoever the browser is signed in as: open a private "
                     f"window, sign in as {expected}, and try again.", False)

    from polling.yt_poller import token_updates
    upd = {_key(k, aid, is_default): v for k, v in token_updates(client).items()}
    if approved_by:
        upd[_key("yt_username", aid, is_default)] = approved_by
    try:
        ch = await client.get_channel()
    except Exception:
        ch = None
    if ch:
        upd[_key("yt_long_uploads", aid, is_default)] = ch.get("long_uploads", "") or ""
    upd["yt_notifications_enabled"] = True
    await client.close()
    config.save_settings(upd)
    _sync_handle(aid, approved_by)
    logger.info("YT: stored a token pair from the authorisation-code flow (account=%s, approved by %s)",
                aid, approved_by or "unconfirmed")
    who = f" It posts as {approved_by}." if approved_by else " YouTube did not say which account approved it."
    return _page("YouTube connected", "The tokens are saved; PawPoller renews them by itself from here. Uploads stay private until the project passes YouTube's audit — flip each one in Studio." + who, True)


def _sync_handle(aid: int | None, handle: str) -> None:
    """The account row's handle follows the approved user, so the Accounts page names it."""
    if not handle:
        return
    from database import accounts as adb
    conn = get_connection()
    try:
        adb.ensure_accounts_table(conn)
        target = aid if aid is not None else adb.get_default_account_id(conn, "yt", create=True)
        if target is not None:
            conn.execute("UPDATE accounts SET handle = ? WHERE account_id = ?", (handle, target))
            conn.commit()
    except Exception:
        logger.debug("YT: could not sync the account handle", exc_info=True)
    finally:
        conn.close()


@yt_router.post("/auth/disconnect")
def sc_disconnect(account_id: int | None = Query(None)):
    """Forget the tokens and the handle; the app credentials stay (the operator's registration)."""
    aid, is_default = _account(account_id)
    config.delete_settings_keys([_key(k, aid, is_default) for k in TOKEN_FIELDS])
    config.save_settings({"yt_notifications_enabled": False})
    return {"status": "success", "message": "YouTube disconnected"}


# ── Polling ──────────────────────────────────────────────────────────────────

@yt_router.get("/poll/progress")
def get_yt_poll_progress():
    return dict(yt_poll_progress)


@yt_router.post("/poll/trigger")
async def trigger_yt_poll():
    try:
        spawn_poll(run_yt_poll_cycle(), "run_yt_poll_cycle")
        return {"status": "started"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error in yt poll trigger: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))


@yt_router.post("/poll/full-resync")
async def sc_full_resync():
    try:
        spawn_poll(run_yt_poll_cycle(force_full=True), "run_yt_poll_cycle full-resync")
        return {"status": "started"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error in yt full resync: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))


# ── Data ─────────────────────────────────────────────────────────────────────

@yt_router.get("/status")
def get_yt_status():
    conn = get_connection()
    try:
        return {
            "total_submissions": conn.execute("SELECT COUNT(*) as c FROM yt_submissions").fetchone()["c"],
            "total_snapshots": conn.execute("SELECT COUNT(*) as c FROM yt_snapshots").fetchone()["c"],
            "last_poll": yt_queries.get_yt_last_poll(conn),
        }
    except Exception as e:
        logger.error("Error in /api/yt/status: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@yt_router.get("/summary")
def get_yt_summary(account_id: int | None = Query(None)):
    conn = get_connection()
    try:
        summary = yt_queries.get_yt_summary(conn, account_id=account_id)
        summary["growth_rates"] = yt_queries.get_yt_growth_rates(conn)
        return summary
    except Exception as e:
        logger.error("Error in /api/yt/summary: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@yt_router.get("/submissions")
def get_yt_submissions(sort_by: str = Query("views"), order: str = Query("desc"), search: str = Query(""),
                       account_id: int | None = Query(None)):
    conn = get_connection()
    try:
        subs = yt_queries.get_all_yt_submissions(conn, sort_by=sort_by, order=order, account_id=account_id)
        deltas = yt_queries.get_yt_submission_deltas(conn)
        if search:
            sl = search.lower()
            subs = [s for s in subs if sl in s["title"].lower() or sl in (s.get("tags") or "").lower()]
        for s in subs:
            d = deltas.get(s["submission_id"], {})
            s["views_delta"] = d.get("views_delta", 0)
            s["favorites_delta"] = d.get("favorites_delta", 0)
            s["comments_delta"] = d.get("comments_delta", 0)
        return {"submissions": subs, "total": len(subs)}
    except Exception as e:
        logger.error("Error in /api/yt/submissions: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@yt_router.get("/submissions/{submission_id}/snapshots")
def get_yt_submission_snapshots(submission_id: str, start: Optional[str] = Query(None),
                                end: Optional[str] = Query(None)):
    conn = get_connection()
    try:
        return {"snapshots": yt_queries.get_yt_snapshots(conn, submission_id, start, end)}
    except Exception as e:
        logger.error("Error in /api/yt/.../snapshots: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@yt_router.get("/submissions/{submission_id}")
def get_yt_submission(submission_id: str):
    conn = get_connection()
    try:
        sub = yt_queries.get_yt_submission(conn, submission_id)
        if not sub:
            raise HTTPException(status_code=404, detail="YouTube track not found")
        try:
            tags = conn.execute(
                "SELECT t.tag_id, t.name, t.color FROM tags t JOIN submission_tags st "
                "ON t.tag_id = st.tag_id WHERE st.platform = 'yt' AND st.submission_id = ?",
                (submission_id,)).fetchall()
        except Exception:
            tags = []
        sub_dict = dict(sub)
        sub_dict["tags"] = [dict(r) for r in tags]
        return {"submission": sub_dict, "snapshots": yt_queries.get_yt_snapshots(conn, submission_id),
                "growth_rates": yt_queries.get_yt_submission_growth_rates(conn, submission_id)}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error in /api/yt/submissions/%s: %s", submission_id[:50], e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@yt_router.get("/aggregate")
def get_yt_aggregate(start: Optional[str] = Query(None), end: Optional[str] = Query(None),
                     account_id: int | None = Query(None)):
    conn = get_connection()
    try:
        return {"snapshots": yt_queries.get_yt_aggregate_snapshots(conn, start, end, account_id=account_id)}
    except Exception as e:
        logger.error("Error in /api/yt/aggregate: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@yt_router.get("/comparison")
def get_yt_comparison(ids: str = Query(...), start: Optional[str] = Query(None), end: Optional[str] = Query(None)):
    conn = get_connection()
    try:
        raw_ids = [x.strip() for x in ids.split(",") if x.strip()]
        if len(raw_ids) > 10:
            raise HTTPException(400, "Max 10 videos for comparison")
        submission_ids, titles = [], {}
        for rid in raw_ids:
            sub = yt_queries.get_yt_submission(conn, rid)
            if sub:
                submission_ids.append(sub["submission_id"])
                titles[sub["submission_id"]] = sub["title"]
        return {"series": yt_queries.get_yt_comparison_snapshots(conn, submission_ids, start, end), "titles": titles}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error in /api/yt/comparison: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@yt_router.get("/poll_log")
def get_yt_poll_log(limit: int = Query(50, ge=1, le=200)):
    conn = get_connection()
    try:
        return {"polls": yt_queries.get_yt_poll_log(conn, limit)}
    except Exception as e:
        logger.error("Error in /api/yt/poll_log: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


# ── CSV export ───────────────────────────────────────────────────────────────

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


@yt_router.get("/export/submissions")
def export_yt_submissions():
    conn = get_connection()
    try:
        return _csv_response(yt_queries.get_all_yt_submissions(conn), "yt_submissions.csv")
    finally:
        conn.close()


@yt_router.get("/export/snapshots")
def export_yt_snapshots(id: str | None = Query(None)):
    conn = get_connection()
    try:
        if id:
            snaps = yt_queries.get_yt_snapshots(conn, id)
        else:
            snaps = [dict(r) for r in conn.execute("SELECT * FROM yt_snapshots ORDER BY polled_at ASC").fetchall()]
        return _csv_response(snaps, f"yt_snapshots{'_' + id[:20] if id else ''}.csv")
    finally:
        conn.close()
