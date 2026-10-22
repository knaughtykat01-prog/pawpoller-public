"""Progress everywhere (WORKLEVELS, spec 052): ``/api/progress``.

Shape: ``specs/052-progress-everywhere/contracts/progress-api.md``. Logic lives in
``progress.py`` (levels, audience XP, performance) and ``medals.py`` (medals, announce).
"""
from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException

import config
import medals
import progress
from database.db import get_connection

progress_router = APIRouter(prefix="/api/progress", tags=["progress"])


def _enabled() -> bool:
    import xp
    return xp.enabled()


@progress_router.get("")
def get_progress():
    conn = get_connection()
    try:
        synced = progress.sync(conn)
        medals.evaluate(conn, synced)
        conn.commit()
        on = _enabled()
        now = datetime.now(config.display_zone())
        monday = (now - timedelta(days=now.weekday())).strftime("%Y-%m-%d")
        week = {   # the one-off back-pay isn't "this week's" level-ups
            "work_levelups": conn.execute(
                "SELECT COUNT(*) FROM xp_ledger WHERE kind = 'audience' AND day >= ? AND earned_at >"
                " COALESCE((SELECT value FROM xp_state WHERE key = 'progress_baselined_at'), '')",
                (monday,)).fetchone()[0],
            "medals": conn.execute("SELECT COUNT(*) FROM earned_medals WHERE source = 'live' AND earned_at >= ?",
                                   (monday,)).fetchone()[0],
        }
        levels = {r[0]: {"subject": r[0], "level": r[1], "score": r[2]} for r in conn.execute(
            "SELECT subject, peak_level, score FROM account_levels")}
        from database import accounts as accounts_db, personas as personas_db
        accts = [levels[f"acct:{a['account_id']}"] for a in accounts_db.list_accounts(conn)
                 if f"acct:{a['account_id']}" in levels]
        pers = [levels[f"persona:{p['persona_id']}"] for p in personas_db.list_personas(conn)
                if f"persona:{p['persona_id']}" in levels]
        return {
            "enabled": on,
            "medals": medals.install_list(conn, synced["scores"]),
            "accounts": accts,
            "personas": pers,
            "week": week,
            "announce": medals.pending(conn, synced["titles"]) if on
            else {"medals": [], "works": [], "catchup_count": 0, "backpay_xp": 0},
        }
    finally:
        conn.close()


@progress_router.get("/work/{content_type}/{name:path}")
def get_work(content_type: str, name: str):
    if content_type not in ("story", "artwork"):
        raise HTTPException(404, "Unknown work")
    conn = get_connection()
    try:
        out = progress.work_detail(conn, content_type, name)
        conn.commit()
    finally:
        conn.close()
    if out is None:
        raise HTTPException(404, "Unknown work")
    return out


@progress_router.post("/announce")
def post_announce(body: dict):
    conn = get_connection()
    try:
        try:
            medals.announce(conn, body or {})
        except (TypeError, ValueError, AttributeError):
            # junk from the browser (a level that isn't a number, a list that isn't one): nothing marked
            raise HTTPException(400, "Malformed announcement")
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()
