"""REST API endpoints for Newgrounds (ng) — MEDIAPLATS §5 (4.23.0).

Cookie session (browser login or a pasted cookie string) + the operator's Newgrounds
username; no API on the site. ``auth/connect`` refuses a cookie that is signed in as
someone else (the 3.31.0 / 4.6.3 FurAffinity lesson). Data routes mirror routes/sc_api.py.
"""
from __future__ import annotations

import csv
import io
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

import config
from database import ng_queries
from database.db import get_connection
from polling.background import spawn_poll
from polling.ng_poller import run_ng_poll_cycle, ng_poll_progress

logger = logging.getLogger(__name__)
ng_router = APIRouter(prefix="/api/ng")

CRED_FIELDS = ("ng_username", "ng_cookie")


def _account(account_id: int | None) -> tuple[int | None, bool]:
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
        return {k: settings.get(k, "") for k in CRED_FIELDS}
    return config.resolve_account_credentials("ng", aid, is_default, settings)


def _key(field: str, account_id: int | None, is_default: bool) -> str:
    return field if account_id is None else config.account_setting_key(account_id, field, is_default)


# ── Auth ─────────────────────────────────────────────────────────────────────

@ng_router.get("/auth/status")
def ng_auth_status(account_id: int | None = Query(None)):
    creds = _creds(account_id)
    has_credentials = bool((creds.get("ng_cookie") or "").strip())
    has_data = False
    conn = get_connection()
    try:
        has_data = conn.execute("SELECT COUNT(*) as c FROM ng_submissions").fetchone()["c"] > 0
    except Exception:
        pass
    finally:
        conn.close()
    return {"has_credentials": has_credentials, "has_data": has_data,
            "username": (creds.get("ng_username") or "").strip(),
            "note": "Newgrounds has no API: PawPoller uses your browser session. mp3 must be 44.1 kHz; "
                    "a new submission goes Under Judgment first."}


@ng_router.post("/auth/connect")
async def ng_connect(body: dict):
    """Save a pasted cookie string + username after checking whose session it is."""
    username = str(body.get("username", "") or "").strip()
    cookie = str(body.get("cookie", "") or "").strip()
    account_id = body.get("account_id")
    aid, is_default = _account(int(account_id) if account_id else None)
    if not cookie:
        raise HTTPException(400, "Paste the Newgrounds cookie string (or use Login via Browser)")
    from clients.ng.client import NgClient
    client = NgClient(username=username, cookie=cookie)
    try:
        session = await client.validate_session()
    except Exception as e:
        raise HTTPException(502, f"Could not reach Newgrounds: {e}")
    finally:
        await client.close()
    if not session.get("logged_in"):
        raise HTTPException(401, session.get("detail") or "That cookie is not signed in to Newgrounds.")
    if not session.get("ok"):
        raise HTTPException(409, session.get("detail") or "That session belongs to a different Newgrounds account.")
    who = session.get("username") or username
    config.save_settings({_key("ng_cookie", aid, is_default): cookie, _key("ng_username", aid, is_default): who,
                          "ng_notifications_enabled": True})
    return {"status": "success", "message": f"Connected — tracking {who}", "username": who}


@ng_router.post("/auth/disconnect")
def ng_disconnect(account_id: int | None = Query(None)):
    aid, is_default = _account(account_id)
    config.delete_settings_keys([_key(k, aid, is_default) for k in CRED_FIELDS])
    config.save_settings({"ng_notifications_enabled": False})
    return {"status": "success", "message": "Newgrounds disconnected"}


# ── Polling ──────────────────────────────────────────────────────────────────

@ng_router.get("/poll/progress")
def get_ng_poll_progress():
    return dict(ng_poll_progress)


@ng_router.post("/poll/trigger")
async def trigger_ng_poll():
    try:
        spawn_poll(run_ng_poll_cycle(), "run_ng_poll_cycle")
        return {"status": "started"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error in ng poll trigger: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))


@ng_router.post("/poll/full-resync")
async def ng_full_resync():
    try:
        spawn_poll(run_ng_poll_cycle(force_full=True), "run_ng_poll_cycle full-resync")
        return {"status": "started"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error in ng full resync: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))


# ── Data ─────────────────────────────────────────────────────────────────────

@ng_router.get("/status")
def get_ng_status():
    conn = get_connection()
    try:
        return {
            "total_submissions": conn.execute("SELECT COUNT(*) as c FROM ng_submissions").fetchone()["c"],
            "total_snapshots": conn.execute("SELECT COUNT(*) as c FROM ng_snapshots").fetchone()["c"],
            "last_poll": ng_queries.get_ng_last_poll(conn),
        }
    except Exception as e:
        logger.error("Error in /api/ng/status: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@ng_router.get("/summary")
def get_ng_summary(account_id: int | None = Query(None)):
    conn = get_connection()
    try:
        summary = ng_queries.get_ng_summary(conn, account_id=account_id)
        summary["growth_rates"] = ng_queries.get_ng_growth_rates(conn)
        return summary
    except Exception as e:
        logger.error("Error in /api/ng/summary: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@ng_router.get("/submissions")
def get_ng_submissions(sort_by: str = Query("views"), order: str = Query("desc"), search: str = Query(""),
                       account_id: int | None = Query(None), portal: str = Query("")):
    conn = get_connection()
    try:
        subs = ng_queries.get_all_ng_submissions(conn, sort_by=sort_by, order=order, account_id=account_id)
        deltas = ng_queries.get_ng_submission_deltas(conn)
        if portal in ("audio", "movie"):
            subs = [s for s in subs if s.get("portal") == portal]
        if search:
            sl = search.lower()
            subs = [s for s in subs if sl in s["title"].lower() or sl in (s.get("genre") or "").lower()]
        for s in subs:
            d = deltas.get(s["submission_id"], {})
            s["views_delta"] = d.get("views_delta", 0)
            s["favorites_delta"] = d.get("favorites_delta", 0)
            s["comments_delta"] = d.get("comments_delta", 0)
        return {"submissions": subs, "total": len(subs)}
    except Exception as e:
        logger.error("Error in /api/ng/submissions: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@ng_router.get("/submissions/{submission_id}/snapshots")
def get_ng_submission_snapshots(submission_id: str, start: Optional[str] = Query(None),
                                end: Optional[str] = Query(None)):
    conn = get_connection()
    try:
        return {"snapshots": ng_queries.get_ng_snapshots(conn, submission_id, start, end)}
    except Exception as e:
        logger.error("Error in /api/ng/.../snapshots: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@ng_router.get("/submissions/{submission_id}")
def get_ng_submission(submission_id: str):
    conn = get_connection()
    try:
        sub = ng_queries.get_ng_submission(conn, submission_id)
        if not sub:
            raise HTTPException(status_code=404, detail="Newgrounds submission not found")
        try:
            tags = conn.execute(
                "SELECT t.tag_id, t.name, t.color FROM tags t JOIN submission_tags st "
                "ON t.tag_id = st.tag_id WHERE st.platform = 'ng' AND st.submission_id = ?",
                (submission_id,)).fetchall()
        except Exception:
            tags = []
        sub_dict = dict(sub)
        sub_dict["tags"] = [dict(r) for r in tags]
        return {"submission": sub_dict, "snapshots": ng_queries.get_ng_snapshots(conn, submission_id),
                "growth_rates": ng_queries.get_ng_submission_growth_rates(conn, submission_id)}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error in /api/ng/submissions/%s: %s", submission_id[:50], e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@ng_router.get("/aggregate")
def get_ng_aggregate(start: Optional[str] = Query(None), end: Optional[str] = Query(None),
                     account_id: int | None = Query(None)):
    conn = get_connection()
    try:
        return {"snapshots": ng_queries.get_ng_aggregate_snapshots(conn, start, end, account_id=account_id)}
    except Exception as e:
        logger.error("Error in /api/ng/aggregate: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@ng_router.get("/comparison")
def get_ng_comparison(ids: str = Query(...), start: Optional[str] = Query(None), end: Optional[str] = Query(None)):
    conn = get_connection()
    try:
        raw_ids = [x.strip() for x in ids.split(",") if x.strip()]
        if len(raw_ids) > 10:
            raise HTTPException(400, "Max 10 submissions for comparison")
        submission_ids, titles = [], {}
        for rid in raw_ids:
            sub = ng_queries.get_ng_submission(conn, rid)
            if sub:
                submission_ids.append(sub["submission_id"])
                titles[sub["submission_id"]] = sub["title"]
        return {"series": ng_queries.get_ng_comparison_snapshots(conn, submission_ids, start, end), "titles": titles}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error in /api/ng/comparison: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@ng_router.get("/poll_log")
def get_ng_poll_log(limit: int = Query(50, ge=1, le=200)):
    conn = get_connection()
    try:
        return {"polls": ng_queries.get_ng_poll_log(conn, limit)}
    except Exception as e:
        logger.error("Error in /api/ng/poll_log: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


# ── CSV export ───────────────────────────────────────────────────────────────

def _sanitize_csv_value(val):
    if isinstance(val, str) and val and val[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + val
    return val


def _csv_response(rows: list[dict], filename: str):
    from fastapi.responses import StreamingResponse
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


@ng_router.get("/export/submissions")
def export_ng_submissions():
    conn = get_connection()
    try:
        return _csv_response(ng_queries.get_all_ng_submissions(conn), "ng_submissions.csv")
    finally:
        conn.close()


@ng_router.get("/export/snapshots")
def export_ng_snapshots(id: str | None = Query(None)):
    conn = get_connection()
    try:
        if id:
            snaps = ng_queries.get_ng_snapshots(conn, id)
        else:
            snaps = [dict(r) for r in conn.execute("SELECT * FROM ng_snapshots ORDER BY polled_at ASC").fetchall()]
        return _csv_response(snaps, f"ng_snapshots{'_' + id[:20] if id else ''}.csv")
    finally:
        conn.close()
