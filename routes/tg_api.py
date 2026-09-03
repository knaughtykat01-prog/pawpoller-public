"""REST API endpoints for the Telegram channel analytics dashboard.

Same shape as every other platform's router, so the dashboard, compare page and
CSV export need no special case. Three things differ, all of them consequences
of what a bot can actually see:

* **No auth routes here.** Telegram's connect/test flow already lives at
  ``/api/settings/telegram/*`` in the core router — it predates this module and
  is what the first-run wizard drives. Adding a second copy would give the app
  two places to set a bot token.
* **One metric.** Reactions. Views are not in the Bot API at all (they are
  client-API only), and channel comments live in a linked discussion group,
  which is a different chat. Responses carry ``reactions_count`` and nothing
  else rather than zero-filled columns that would look like real measurements.
* **``uncounted`` travels with every total.** Reactions arrive only as pushed
  updates and cannot be backfilled, so posts made before tracking started have
  no count and never will. Reporting the total without that number turns a
  short observation window into what looks like poor engagement.

Submissions are exact rather than polled: PawPoller sent each post and recorded
its message id, so ``/submissions`` lists what we actually published rather than
whatever a site chooses to return.
"""

from __future__ import annotations
import csv
import io
import json
import logging
from typing import Optional

from fastapi import APIRouter, Query, HTTPException
from fastapi.responses import StreamingResponse

from database.db import get_connection
from database import tg_queries
from polling.tg_poller import run_tg_poll_cycle, progress as tg_poll_progress
from polling.background import spawn_poll

logger = logging.getLogger(__name__)
tg_router = APIRouter(prefix="/api/tg")


# -- Telegram Polling -----------------------------------------------------------
#
# A "poll" here fetches the channel's subscriber count and nothing else — see
# polling/tg_poller.py for why that is the whole of what a cycle can do.


@tg_router.get("/poll/progress")
def get_tg_poll_progress():
    return dict(tg_poll_progress)


@tg_router.post("/poll/trigger")
async def trigger_tg_poll():
    try:
        spawn_poll(run_tg_poll_cycle(), "run_tg_poll_cycle")
        return {"status": "started"}
    # Let an explicit HTTPException through — the ownership guard in spawn_poll
    # raises 409 here, and the blanket handler below would otherwise report it
    # as a 500 'internal error'.
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error in tg poll trigger: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))


@tg_router.post("/poll/full-resync")
async def tg_full_resync():
    """Accepted for parity with every other platform, but there is no
    back-catalogue to re-fetch: reactions cannot be queried and subscriber
    counts have no history to re-read. It runs an ordinary cycle."""
    try:
        spawn_poll(run_tg_poll_cycle(force_full=True), "run_tg_poll_cycle full-resync")
        return {"status": "started"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error in tg full resync: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))


# -- Telegram Data --------------------------------------------------------------


@tg_router.get("/status")
def get_tg_status():
    conn = get_connection()
    try:
        return {
            "total_submissions": conn.execute(
                "SELECT COUNT(*) c FROM tg_submissions").fetchone()["c"],
            "total_snapshots": conn.execute(
                "SELECT COUNT(*) c FROM tg_snapshots").fetchone()["c"],
            "last_poll": tg_queries.get_tg_last_poll(conn),
        }
    except Exception as e:
        logger.error("Error in /api/tg/status: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@tg_router.get("/summary")
def get_tg_summary(account_id: int | None = Query(None)):
    conn = get_connection()
    try:
        return tg_queries.get_dashboard_summary(conn, account_id=account_id)
    except Exception as e:
        logger.error("Error in /api/tg/summary: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@tg_router.get("/submissions")
def get_tg_submissions(
    sort_by: str = Query("posted_at", description="Sort field"),
    order: str = Query("desc", description="Sort order"),
    search: str = Query("", description="Search title"),
    account_id: int | None = Query(None),
):
    conn = get_connection()
    try:
        subs = tg_queries.get_all_submissions(
            conn, sort_by=sort_by, order=order, account_id=account_id)
        deltas = tg_queries.get_deltas(conn)

        if search:
            needle = search.lower()
            subs = [s for s in subs if needle in (s.get("title") or "").lower()]

        for s in subs:
            s["reactions_delta"] = deltas.get(s["submission_id"], {}).get("reactions_delta", 0)
            # The flag the UI needs to render "not counted" instead of 0. A post
            # sent before reaction tracking began has never been observed, which
            # is a different statement from "nobody reacted" — and only this
            # column can tell them apart.
            s["reactions_counted"] = s.get("reactions_at") is not None

        return {"submissions": subs, "total": len(subs)}
    except Exception as e:
        logger.error("Error in /api/tg/submissions: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


# Registered BEFORE the bare /submissions/{id} route below. The `:path`
# converter is greedy and Starlette matches in registration order, so with
# the bare route first this URL resolved to a submission id of
# "<id>/snapshots" and returned 404 for every post that exists.
@tg_router.get("/submissions/{submission_id:path}/snapshots")
def get_tg_submission_snapshots(
    submission_id: str,
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
):
    conn = get_connection()
    try:
        series = tg_queries.get_comparison_snapshots(
            conn, [submission_id], start, end).get(submission_id, [])
        return {"snapshots": series}
    except Exception as e:
        logger.error("Error in /api/tg/submissions/%s/snapshots: %s",
                     submission_id[:50], e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@tg_router.get("/submissions/{submission_id:path}")
def get_tg_submission(submission_id: str):
    conn = get_connection()
    try:
        sub = tg_queries.get_submission(conn, submission_id)
        if not sub:
            raise HTTPException(404, detail="Telegram post not found")
        sub["reactions_counted"] = sub.get("reactions_at") is not None
        # Per-emoji breakdown, parsed for the UI. Stored as JSON rather than a
        # column per emoji because a channel can use any of them, including
        # custom ones.
        try:
            sub["reactions_breakdown"] = json.loads(sub.get("reactions_json") or "[]")
        except (TypeError, ValueError):
            sub["reactions_breakdown"] = []
        try:
            tags = conn.execute(
                "SELECT t.tag_id, t.name, t.color FROM tags t"
                " JOIN submission_tags st ON t.tag_id = st.tag_id"
                " WHERE st.platform = 'tg' AND st.submission_id = ?",
                (sub["submission_id"],)).fetchall()
        except Exception:
            tags = []
        sub["tags"] = [dict(r) for r in tags]
        return {
            "submission": sub,
            "snapshots": tg_queries.get_snapshots(conn, sub["submission_id"]),
            "growth_rates": tg_queries.get_growth_rates(conn),
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error in /api/tg/submissions/%s: %s", submission_id[:50], e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@tg_router.get("/aggregate")
def get_tg_aggregate(
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
    account_id: int | None = Query(None),
):
    conn = get_connection()
    try:
        return {"snapshots": tg_queries.get_aggregate_snapshots(
            conn, start, end, account_id=account_id)}
    except Exception as e:
        logger.error("Error in /api/tg/aggregate: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@tg_router.get("/comparison")
def get_tg_comparison(
    ids: str = Query(..., description="Comma-separated submission ids"),
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
):
    conn = get_connection()
    try:
        raw_ids = [x.strip() for x in ids.split(",") if x.strip()]
        if len(raw_ids) > 10:
            raise HTTPException(400, "Max 10 posts for comparison")

        submission_ids, titles = [], {}
        for rid in raw_ids:
            sub = tg_queries.get_submission(conn, rid)
            if sub:
                submission_ids.append(sub["submission_id"])
                titles[sub["submission_id"]] = sub["title"]

        return {"series": tg_queries.get_comparison_snapshots(
            conn, submission_ids, start, end), "titles": titles}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error in /api/tg/comparison: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@tg_router.get("/poll_log")
def get_tg_poll_log(limit: int = Query(50, ge=1, le=200)):
    conn = get_connection()
    try:
        return {"polls": tg_queries.get_tg_poll_log(conn, limit)}
    except Exception as e:
        logger.error("Error in /api/tg/poll_log: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


# -- Telegram CSV Export --------------------------------------------------------

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


@tg_router.get("/export/submissions")
def export_tg_submissions():
    conn = get_connection()
    try:
        subs = tg_queries.get_all_submissions(conn)
        # An empty reactions column would read as "0 reactions" in a
        # spreadsheet, which is the one thing this platform's data must never
        # imply. Say so in words instead.
        for s in subs:
            if s.get("reactions_at") is None:
                s["reactions_count"] = "not counted"
        return _csv_response(subs, "tg_submissions.csv")
    finally:
        conn.close()


@tg_router.get("/export/snapshots")
def export_tg_snapshots(id: str | None = Query(None)):
    conn = get_connection()
    try:
        if id:
            snaps = tg_queries.get_snapshots(conn, id)
        else:
            snaps = [dict(r) for r in conn.execute(
                "SELECT * FROM tg_snapshots ORDER BY polled_at ASC").fetchall()]
        return _csv_response(snaps, f"tg_snapshots{'_' + id[:20] if id else ''}.csv")
    finally:
        conn.close()
