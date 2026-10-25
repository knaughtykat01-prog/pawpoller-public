"""REST API for Twitch (ttv) + its go-live posts — spec 045.

Tracking (Picarto's set) under /api/ttv; the channel is saved only after Twitch resolves it, and is
stored by its permanent id. Connect (optional) is Twitch's authorization-code flow for followers,
subscribers and Bits. Go-live: the channel's switches + targets, the recent runs, Post / Skip.

``/hooks/twitch`` (``ttv_hooks_router``) is Twitch's EventSub webhook: auth-exempt like /hooks/trello,
so the route itself is the gate — HMAC signature with this install's secret, a 10-minute timestamp
window, a replay guard on the message id, a body cap. See contracts/webhook.md.
"""
from __future__ import annotations

import asyncio
import csv
import hashlib
import hmac
import io
import json
import logging
import secrets
import time
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response, StreamingResponse

import config
from database import ttv_queries
from database.db import get_connection
from database.queries import thin_series
from polling import golive
from polling.background import spawn_poll
from polling.ttv_poller import TOKEN_FIELDS, make_client, run_ttv_poll_cycle, save_tokens, ttv_poll_progress
from routes.body_caps import read_capped

logger = logging.getLogger(__name__)
ttv_router = APIRouter(prefix="/api/ttv")
ttv_hooks_router = APIRouter()

_oauth_state: dict[str, dict] = {}       # state → {"at", "account_id", "is_default"}; single-use, in-process
_STATE_TTL = 900
_tasks: set = set()


def _account(account_id: int | None) -> tuple[int, bool]:
    """(account_id, is_default) — always a real account (go-live rows are keyed by it)."""
    from database import accounts as adb
    conn = get_connection()
    try:
        if not account_id:
            account_id = adb.get_default_account_id(conn, "ttv", create=True)
        acct = adb.get_account(conn, int(account_id))
    finally:
        conn.close()
    if not acct or acct["platform"] != "ttv":
        raise HTTPException(404, f"No Twitch account {account_id}")
    return int(account_id), bool(acct["is_default"])


def _key(field: str, aid: int, is_default: bool) -> str:
    return config.account_setting_key(aid, field, is_default)


def _creds(aid: int, is_default: bool) -> dict:
    return config.resolve_account_credentials("ttv", aid, is_default, config.get_settings())


def _redirect_uri(request: Request) -> str:
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    return f"{proto}://{host}/api/ttv/auth/callback"


def _listening() -> str:
    return "webhook" if golive.webhook_base() else "check"


# ── Channel + app ────────────────────────────────────────────────────────────

@ttv_router.get("/channel/status")
def ttv_channel_status(account_id: int | None = Query(None)):
    aid, is_default = _account(account_id)
    creds = _creds(aid, is_default)
    client = make_client(creds)
    conn = get_connection()
    try:
        ch = golive.get_channel(conn, "ttv", aid)
    finally:
        conn.close()
    return {"has_credentials": bool(creds.get("ttv_user_id")), "channel": creds.get("ttv_channel", ""),
            "user_id": creds.get("ttv_user_id", ""), "has_app": client.has_app,
            "app_from_env": client.has_app and not creds.get("ttv_client_id"),
            "client_id": creds.get("ttv_client_id", ""), "connected": client.connected,
            "login": creds.get("ttv_login", ""), "listening": _listening(), "live": ch["live_state"] == "live",
            "account_id": aid}


@ttv_router.post("/channel")
async def ttv_save_channel(body: dict):
    """Save the app key (if given), then the channel — only once Twitch says it exists."""
    from clients.ttv.client import TtvAuthError, TtvError, clean_login
    aid, is_default = _account(body.get("account_id"))
    login = clean_login(str(body.get("channel") or ""))
    if not login:
        raise HTTPException(400, "That isn't a Twitch channel name — it's the last part of your twitch.tv address.")
    upd = {}
    cid, secret = str(body.get("client_id") or "").strip(), str(body.get("client_secret") or "").strip()
    if cid:
        upd[_key("ttv_client_id", aid, is_default)] = cid
    if secret:
        upd[_key("ttv_client_secret", aid, is_default)] = secret
    creds = {**_creds(aid, is_default), **({"ttv_client_id": cid} if cid else {}),
             **({"ttv_client_secret": secret} if secret else {})}
    client = make_client(creds)
    if not client.has_app:
        raise HTTPException(400, "Add your Twitch app's client id and secret first (the guide shows where).")
    try:
        user = await client.get_user(login)
    except TtvAuthError as e:
        raise HTTPException(400, str(e))
    except TtvError as e:
        raise HTTPException(502, str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"Couldn't reach Twitch: {type(e).__name__}")
    finally:
        await client.close()
    if not user:
        raise HTTPException(400, f"No Twitch channel called {login}.")
    if creds.get("ttv_user_id") and creds["ttv_user_id"] != user["user_id"]:
        # A different channel: the old one's Connect doesn't carry over.
        config.delete_settings_keys([_key(k, aid, is_default) for k in (*TOKEN_FIELDS, "ttv_login")])
    upd[_key("ttv_channel", aid, is_default)] = user["login"]
    upd[_key("ttv_user_id", aid, is_default)] = user["user_id"]
    upd["ttv_notifications_enabled"] = True
    config.save_settings(upd)
    _sync_handle(aid, user["login"])
    logger.info("Twitch channel saved (account=%s)", aid)
    return {"status": "success", "channel": user["login"], "display_name": user["name"], "user_id": user["user_id"]}


def _sync_handle(aid: int, handle: str) -> None:
    from database import accounts as adb
    conn = get_connection()
    try:
        conn.execute("UPDATE accounts SET handle = ? WHERE account_id = ?", (handle, aid))
        conn.commit()
    except Exception:
        logger.debug("Twitch: could not sync the account handle", exc_info=True)
    finally:
        conn.close()


# ── Connect (optional: followers, subscribers, Bits) ─────────────────────────

@ttv_router.get("/auth/authorize-url")
def ttv_authorize_url(request: Request, account_id: int | None = Query(None)):
    from clients.ttv.client import authorize_url
    aid, is_default = _account(account_id)
    creds = _creds(aid, is_default)
    client = make_client(creds)
    if not client.has_app:
        raise HTTPException(400, "Add your Twitch app's client id and secret first")
    if not creds.get("ttv_user_id"):
        raise HTTPException(400, "Save your channel first")
    now = time.time()
    for old in [s for s, v in _oauth_state.items() if now - v["at"] > _STATE_TTL]:
        _oauth_state.pop(old, None)
    state = secrets.token_urlsafe(24)
    _oauth_state[state] = {"at": now, "account_id": aid, "is_default": is_default}
    redirect = _redirect_uri(request)
    return {"url": authorize_url(client.client_id, redirect, state), "redirect_uri": redirect}


def _page(title: str, detail: str, ok: bool) -> HTMLResponse:
    from html import escape
    colour = "#3fb950" if ok else "#f85149"
    return HTMLResponse(
        f"<html><head><title>{escape(title)}</title></head><body style='font-family:system-ui,sans-serif;"
        f"background:#0d1117;color:#c9d1d9;padding:48px;line-height:1.6'><h2 style='color:{colour}'>{escape(title)}"
        f"</h2><p>{escape(detail)}</p><p style='color:#8b949e;font-size:13px'>You can close this tab.</p></body></html>",
        status_code=200 if ok else 400)


@ttv_router.get("/auth/callback")
async def ttv_auth_callback(request: Request, code: str = "", state: str = "", error: str = "",
                            error_description: str = ""):
    if error:
        return _page("Connection refused", f"Twitch said: {error_description or error}", False)
    entry = _oauth_state.pop(state, None) if state else None     # single-use, whatever happens next
    if not code or not entry or time.time() - entry["at"] > _STATE_TTL:
        return _page("Connection failed", "That approval did not come from this PawPoller, or it expired. "
                                          "Start again from Settings.", False)
    aid, is_default = entry["account_id"], entry["is_default"]
    creds = _creds(aid, is_default)
    client = make_client({**creds, "ttv_access_token": "", "ttv_refresh_token": ""})
    try:
        await client.exchange_code(code, _redirect_uri(request))
        who = await client.validate_user()
    except Exception as e:  # noqa: BLE001
        await client.close()
        return _page("Connection failed", str(e) or type(e).__name__, False)
    await client.close()
    # Twitch approves whoever the browser is signed in as — refuse a different channel's approval.
    if not who or who["user_id"] != str(creds.get("ttv_user_id")):
        return _page("Wrong Twitch account",
                     f"That approval came from {who['login'] if who else 'another account'}, not {creds.get('ttv_channel')}."
                     " Nothing was saved. Sign in to Twitch as the channel's owner and try again.", False)
    save_tokens(client, aid, is_default)
    config.save_settings({_key("ttv_login", aid, is_default): who["login"]})
    return _page("Twitch connected", "Followers, subscribers and Bits appear after the next poll.", True)


@ttv_router.post("/auth/disconnect")
def ttv_disconnect(account_id: int | None = Query(None)):
    aid, is_default = _account(account_id)
    config.delete_settings_keys([_key(k, aid, is_default) for k in (*TOKEN_FIELDS, "ttv_login")])
    return {"status": "success", "message": "Twitch disconnected — the channel stays watched"}


# ── Go-live ──────────────────────────────────────────────────────────────────

@ttv_router.get("/golive")
def ttv_golive_get(account_id: int | None = Query(None)):
    aid, _ = _account(account_id)
    conn = get_connection()
    try:
        return {"channel": golive.get_channel(conn, "ttv", aid), "targets": golive.get_targets(conn, "ttv", aid),
                "available": golive.available_targets(conn, config.get_settings()),
                "placeholders": list(golive.PLACEHOLDERS), "listening": _listening(),
                "defaults": {"live": golive.DEFAULT_LIVE, "ended": golive.DEFAULT_ENDED},
                "editable": list(golive.EDITABLE), "account_id": aid}
    finally:
        conn.close()


@ttv_router.put("/golive")
def ttv_golive_put(body: dict):
    aid, _ = _account(body.get("account_id"))
    try:
        return golive.save_config("ttv", aid, body)
    except ValueError as e:
        raise HTTPException(422, str(e))


@ttv_router.get("/golive/runs")
def ttv_golive_runs(limit: int = Query(20, ge=1, le=100)):
    return {"runs": [r for r in golive.list_runs(limit) if r["site"] == "ttv"]}


async def _decide(run_id: int, action: str):
    conn = get_connection()
    try:
        run = golive.get_run(conn, run_id)
    finally:
        conn.close()
    if not run or run["site"] != "ttv":
        raise HTTPException(404, "No such go-live")
    if run["state"] != "waiting":
        raise HTTPException(409, {"state": run["state"], "detail": f"That go-live is already {run['state']}."})
    state = await golive.decide(run_id, action, by="app")
    if state == "expired":
        raise HTTPException(409, {"state": state, "detail": "That stream's go-live expired."})
    return {"state": state}


@ttv_router.post("/golive/runs/{run_id}/post")
async def ttv_golive_post(run_id: int):
    return await _decide(run_id, "post")


@ttv_router.post("/golive/runs/{run_id}/skip")
async def ttv_golive_skip(run_id: int):
    return await _decide(run_id, "skip")


@ttv_router.post("/golive/test")
def ttv_golive_test(body: dict):
    aid, _ = _account(body.get("account_id"))
    return {"posts": golive.preview("ttv", aid)}


# ── Twitch's webhook ─────────────────────────────────────────────────────────

_MAX_BODY = 64 * 1024


def _when(ts: str) -> float | None:
    try:
        return datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
    except (TypeError, ValueError):
        return None


_last_refusal_log = 0.0


def _log_refusal() -> None:
    """At most one line a minute, as /hooks/trello: a stranger shouldn't get to fill the log."""
    global _last_refusal_log
    now = time.monotonic()
    if now - _last_refusal_log >= 60:
        _last_refusal_log = now
        logger.warning("Twitch webhook: a delivery with a bad signature was refused")


@ttv_hooks_router.post("/hooks/twitch")
async def twitch_webhook(request: Request):
    if not golive.webhook_base():
        raise HTTPException(404)
    # Streamed with a cap: a chunked body has no Content-Length to check (REVIEW4811, spec 054).
    raw = await read_capped(request, _MAX_BODY)
    h = request.headers
    mid, ts = h.get("twitch-eventsub-message-id", ""), h.get("twitch-eventsub-message-timestamp", "")
    sig, kind = h.get("twitch-eventsub-message-signature", ""), h.get("twitch-eventsub-message-type", "")
    if not (mid and ts and sig and kind):
        raise HTTPException(400)
    want = "sha256=" + hmac.new(golive.eventsub_secret().encode(), mid.encode() + ts.encode() + raw,
                                hashlib.sha256).hexdigest()
    if not hmac.compare_digest(want, sig):
        _log_refusal()
        raise HTTPException(403)
    when = _when(ts)
    if when is None or not (-60 <= time.time() - when <= 600):
        raise HTTPException(403)
    if golive.seen_signal(mid):
        return Response(status_code=204)
    try:
        body = json.loads(raw)
    except ValueError:
        raise HTTPException(400)
    if kind == "webhook_callback_verification":
        return PlainTextResponse(str(body.get("challenge") or ""))
    sub = body.get("subscription") or {}
    uid = str(((sub.get("condition") or {}).get("broadcaster_user_id")) or "")
    if kind == "revocation":
        golive.mark_revoked(uid)
        return Response(status_code=204)
    if kind != "notification":
        return Response(status_code=204)
    aid = golive.account_for_user("ttv", uid)
    if aid is None:
        return Response(status_code=204)
    ev = body.get("event") or {}
    if sub.get("type") == "stream.online":
        # The webhook doesn't carry the title or game: one /streams read fills them, then the engine runs
        # off the request (Twitch wants its answer within seconds).
        task = asyncio.ensure_future(_online_from_signal(aid, ev))
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)
    elif sub.get("type") == "stream.offline":
        golive.on_offline("ttv", aid)
    return Response(status_code=204)


async def _online_from_signal(aid: int, ev: dict) -> None:
    from clients.ttv.client import SITE
    from polling.ttv_poller import account_creds
    creds, _ = account_creds(aid)
    login = str(ev.get("broadcaster_user_login") or "")
    info = {"stream_key": str(ev.get("id") or ""), "started_at": str(ev.get("started_at") or "").replace("T", " ")[:19],
            "name": str(ev.get("broadcaster_user_name") or login), "link": f"{SITE}/{login}" if login else "",
            "title": "", "game": "", "mature": False}
    client = make_client(creds)
    try:
        live = (await client.get_streams([creds.get("ttv_user_id", "")])).get(creds.get("ttv_user_id", ""))
        if live:
            info.update(title=live["title"], game=live["game"], mature=live["mature"])
    except Exception as e:  # noqa: BLE001 — announce with what the signal carried
        logger.info("Twitch: stream details not read (%s)", e)
    finally:
        await client.close()
    try:
        await golive.on_online("ttv", aid, info, detected_by="signal")
    except Exception:
        logger.warning("go-live from the Twitch webhook failed", exc_info=True)


# ── Polling ──────────────────────────────────────────────────────────────────

@ttv_router.get("/poll/progress")
def get_ttv_poll_progress():
    return dict(ttv_poll_progress)


@ttv_router.post("/poll/trigger")
async def trigger_ttv_poll():
    spawn_poll(run_ttv_poll_cycle(), "run_ttv_poll_cycle")
    return {"status": "started"}


@ttv_router.post("/poll/full-resync")
async def ttv_full_resync():
    spawn_poll(run_ttv_poll_cycle(force_full=True), "run_ttv_poll_cycle full-resync")
    return {"status": "started"}


# ── Data ─────────────────────────────────────────────────────────────────────

@ttv_router.get("/status")
def get_ttv_status():
    conn = get_connection()
    try:
        return {
            "total_submissions": conn.execute("SELECT COUNT(*) AS c FROM ttv_submissions").fetchone()["c"],
            "total_snapshots": conn.execute("SELECT COUNT(*) AS c FROM ttv_channel_snapshots").fetchone()["c"],
            "last_poll": ttv_queries.get_ttv_last_poll(conn),
        }
    finally:
        conn.close()


@ttv_router.get("/summary")
def get_ttv_summary(account_id: int | None = Query(None)):
    conn = get_connection()
    try:
        return ttv_queries.get_ttv_summary(conn, account_id=account_id)
    finally:
        conn.close()


@ttv_router.get("/submissions")
def get_ttv_submissions(sort_by: str = Query("posted_at"), order: str = Query("desc"), search: str = Query(""),
                        kind: str = Query(""), account_id: int | None = Query(None)):
    conn = get_connection()
    try:
        subs = ttv_queries.get_all_ttv_items(conn, sort_by=sort_by, order=order, account_id=account_id, kind=kind)
    finally:
        conn.close()
    if search:
        sl = search.lower()
        subs = [s for s in subs if sl in (s["title"] or "").lower()]
    return {"submissions": subs, "total": len(subs)}


@ttv_router.get("/submissions/{submission_id}/snapshots")
def get_ttv_submission_snapshots(submission_id: str):
    """Declared before the detail route so the path isn't swallowed by it."""
    conn = get_connection()
    try:
        return {"snapshots": [dict(r) for r in conn.execute(
            "SELECT * FROM ttv_snapshots WHERE submission_id = ? ORDER BY polled_at", (submission_id,)).fetchall()]}
    finally:
        conn.close()


@ttv_router.get("/submissions/{submission_id}")
def get_ttv_submission(submission_id: str):
    conn = get_connection()
    try:
        sub = ttv_queries.get_ttv_item(conn, submission_id)
        if not sub:
            raise HTTPException(404, "Twitch broadcast or clip not found")
        try:
            tags = conn.execute(
                "SELECT t.tag_id, t.name, t.color FROM tags t JOIN submission_tags st "
                "ON t.tag_id = st.tag_id WHERE st.platform = 'ttv' AND st.submission_id = ?",
                (submission_id,)).fetchall()
        except Exception:
            tags = []
        sub["tags"] = [dict(r) for r in tags]
        return {"submission": sub}
    finally:
        conn.close()


@ttv_router.get("/aggregate")
def get_ttv_aggregate(start: Optional[str] = Query(None), end: Optional[str] = Query(None),
                      account_id: int | None = Query(None)):
    conn = get_connection()
    try:
        return {"snapshots": thin_series(ttv_queries.get_ttv_aggregate(conn, start, end, account_id=account_id))}
    finally:
        conn.close()


@ttv_router.get("/poll_log")
def get_ttv_poll_log(limit: int = Query(50, ge=1, le=200)):
    conn = get_connection()
    try:
        return {"polls": ttv_queries.get_ttv_poll_log(conn, limit)}
    finally:
        conn.close()


def _cell(val):
    """Neutralise spreadsheet formulas in exported text."""
    if isinstance(val, str) and val and val[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + val
    return val


def _csv(rows: list[dict], filename: str) -> StreamingResponse:
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    if not rows:
        return StreamingResponse(iter(["No data"]), media_type="text/csv", headers=headers)
    out = io.StringIO()
    w = csv.DictWriter(out, fieldnames=rows[0].keys())
    w.writeheader()
    w.writerows({k: _cell(v) for k, v in r.items()} for r in rows)
    return StreamingResponse(iter([out.getvalue()]), media_type="text/csv", headers=headers)


@ttv_router.get("/export/submissions")
def export_ttv_submissions():
    conn = get_connection()
    try:
        return _csv(ttv_queries.get_all_ttv_items(conn), "twitch_broadcasts_and_clips.csv")
    finally:
        conn.close()


@ttv_router.get("/export/snapshots")
def export_ttv_snapshots():
    conn = get_connection()
    try:
        return _csv(ttv_queries.get_ttv_channel_snapshots(conn), "twitch_channel.csv")
    finally:
        conn.close()
