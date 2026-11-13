"""Supporter totals — spec 084, contracts/pat-api.md "Supporters". Read-only; totals only, never a person.

All three read ``database/supporter_queries.py``, only rows whose account still exists. Money is never summed across
accounts or currencies. Locked on an open instance (``/api/supporters`` is on the sensitive list).
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from database import supporter_queries
from database.db import get_connection

supporters_router = APIRouter(prefix="/api/supporters")


def _last_errors(conn) -> dict:
    out = {}
    for r in conn.execute("SELECT account_id, status, error_message FROM pat_poll_log WHERE id IN "
                          "(SELECT MAX(id) FROM pat_poll_log GROUP BY account_id)"):
        if r["status"] in ("error", "partial") and r["error_message"]:
            out[r["account_id"]] = r["error_message"]
    return out


@supporters_router.get("/summary")
def supporters_summary():
    conn = get_connection()
    try:
        rows = supporter_queries.summary(conn)
        errors = _last_errors(conn)
    finally:
        conn.close()
    for r in rows:
        r["last_error"] = errors.get(r["account_id"], "")
    return {"accounts": rows, "money_note": "As Patreon shows it.",
            "tier_note": "Tier amounts are before Patreon's adjustments, so they needn't add up to the total."}


@supporters_router.get("/history")
def supporters_history(account_id: int = Query(...), days: int = Query(90)):
    conn = get_connection()
    try:
        return {"snapshots": supporter_queries.history(conn, account_id, days)}
    except ValueError as e:
        raise HTTPException(400, str(e))
    finally:
        conn.close()


@supporters_router.get("/period")
def supporters_period(account_id: int = Query(...), start: str = Query(..., alias="from"),
                      end: str = Query(..., alias="to")):
    conn = get_connection()
    try:
        return supporter_queries.period(conn, account_id, start, end)
    except ValueError as e:
        raise HTTPException(400, str(e))
    finally:
        conn.close()
