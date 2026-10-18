"""Go-live posts — spec 045. "I went live on Twitch" → one templated post on each site the owner ticked.

Site-neutral: a watched site ('ttv' today) reports a stream through ``on_online`` / ``on_offline`` /
``on_check_state``, from either of two places (plan R3):

  * Twitch's EventSub webhook (``routes/ttv_api.py`` ``/hooks/twitch``) on installs with a public https
    address — seconds after the stream starts;
  * ``tick()``'s live check every minute (every 10 when the webhook is known to work), and every stats poll.

Both land in the same idempotent code, so a stream makes at most ONE run however many times it is
reported (``UNIQUE`` on the stream key and on the run's stream). A run is a stream's announcement:

  waiting ─ Post (app / Telegram) ─→ posting → posted | partial | failed
     │─ Skip → skipped     │─ stream ended / 2 h passed → expired
  held   (posting paused, the copy moved away, or the site paused on the box — recorded, never sent)

"Show me first" (on by default) makes every run wait for the owner; Telegram's Post / Skip buttons are
accepted only from the owner's own private chat, once. Without it, a run posts at once — but only while
Settings → Posting's automatic posting is on (``posting_enabled``): a tap on Post is the owner posting by
hand, like the Posts page, and needs no switch.

Wording is the owner's own template per site, with {title} {game} {link} {name} filled in — nothing is
written for them (no generative text, constitution I). Account 10-style "Never post" accounts are never
offered and are refused again at send time. Nothing here deletes a post; the end-of-stream pass only
edits, and only on "edit when it ends".
"""
from __future__ import annotations

import asyncio
import logging
import re
import secrets
import time
from datetime import datetime, timezone

import httpx

import config
from database.db import get_connection

logger = logging.getLogger(__name__)

SITES = {"ttv": "Twitch"}
PLACEHOLDERS = ("title", "game", "link", "name")
DEFAULT_LIVE = "I'm live on {site}! {title} — come hang out: {link}"
DEFAULT_ENDED = "That's the end of the stream — thanks for watching! Next time: {link}"
CATCHUP_HOURS = 2           # a stream first seen this long after it started is recorded, not announced (FR-005)
EXPIRY_HOURS = 2            # a waiting run lapses after this, or when the stream is over (FR-015b)
MAX_COOLDOWN = 60
SIGNAL_KEEP_S = 2 * 86400
CHECK_EVERY_S = 60
CHECK_EVERY_WITH_WEBHOOK_S = 600
RECONCILE_EVERY_S = 600
EDITABLE = ("discord", "mast", "tg")     # where the end-of-stream edit can reach (plan R7)
_LIVE_SUB = ("enabled", "webhook_callback_verification_pending")

_last_check: dict[tuple[str, int], float] = {}
_last_reconcile: float = 0.0


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _parse(ts: str | None) -> datetime | None:
    try:
        return datetime.strptime(str(ts)[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _age_s(ts: str | None) -> float:
    t = _parse(ts)
    return (datetime.now(timezone.utc) - t).total_seconds() if t else 0.0


def render(template: str, stream: dict, site: str = "ttv") -> str:
    """The owner's wording with the stream's details filled in. Anything that isn't one of the
    placeholders is left exactly as typed (a stray "{" in a template must not break a post)."""
    values = {"title": stream.get("title") or "", "game": stream.get("game") or "",
              "link": stream.get("link") or "", "name": stream.get("name") or "",
              "site": SITES.get(site, site)}
    return re.sub(r"\{(title|game|link|name|site)\}", lambda m: values[m.group(1)], template or "").strip()


# ── Settings: the channel's switches + its targets ───────────────────────────

def get_channel(conn, site: str, account_id: int) -> dict:
    row = conn.execute("SELECT * FROM golive_channels WHERE site = ? AND account_id = ?",
                       (site, account_id)).fetchone()
    if row:
        return dict(row)
    return {"site": site, "account_id": account_id, "enabled": 0, "approve_first": 1, "cooldown_min": 15,
            "end_action": "leave", "live_state": "", "checked_at": None, "sub_state": "", "sub_checked_at": 0}


def get_targets(conn, site: str, account_id: int) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM golive_targets WHERE site = ? AND account_id = ? ORDER BY id", (site, account_id))]


def available_targets(conn, settings: dict) -> list[dict]:
    """Where a go-live can be announced: every enabled Posts account that isn't "Never post", plus
    the Discord announce webhook when one is set."""
    from database import accounts as accounts_db
    from posting import post_publisher as pp
    never = accounts_db.never_post_ids(settings)
    out = []
    for a in accounts_db.list_accounts(conn, enabled_only=True):
        if a["platform"] in pp.SUPPORTED and a["account_id"] not in never:
            out.append({"platform": a["platform"], "account_id": a["account_id"],
                        "label": f"{pp.LABELS.get(a['platform'], a['platform'])}"
                                 + (f" · {a['handle'] or a['label']}" if (a.get("handle") or a.get("label")) else ""),
                        "general_only": a["platform"] in pp._GENERAL_ONLY})
    from posting import discord
    if discord.is_configured(settings):
        out.append({"platform": "discord", "account_id": 0, "label": "Discord (your announce channel)",
                    "general_only": False})
    return out


def save_config(site: str, account_id: int, body: dict, settings: dict | None = None) -> dict:
    """Validate and store the channel's switches and targets (replacing the old targets). ValueError on a bad one."""
    settings = settings if settings is not None else config.get_settings()
    try:
        cooldown = int(body.get("cooldown_min", 15))
    except (TypeError, ValueError):
        raise ValueError("The cool-down must be a number of minutes.")
    if not 0 <= cooldown <= MAX_COOLDOWN:
        raise ValueError(f"The cool-down must be between 0 and {MAX_COOLDOWN} minutes.")
    end_action = body.get("end_action", "leave")
    if end_action not in ("leave", "edit"):
        raise ValueError("When the stream ends: choose leave them or edit them.")
    conn = get_connection()
    try:
        allowed = {(t["platform"], t["account_id"]) for t in available_targets(conn, settings)}
        targets = []
        for t in body.get("targets") or []:
            key = (str(t.get("platform") or ""), int(t.get("target_account_id") or t.get("account_id") or 0))
            if key not in allowed:
                raise ValueError("One of the chosen sites can't take go-live posts (not connected, or marked "
                                 "Never post).")
            live = str(t.get("live_template") or "").strip() or DEFAULT_LIVE
            if len(live) > 2000 or len(str(t.get("ended_template") or "")) > 2000:
                raise ValueError("A message is too long (2,000 characters at most).")
            targets.append((*key, live, str(t.get("ended_template") or "").strip() or DEFAULT_ENDED,
                            1 if t.get("allow_mature") else 0))
        conn.execute(
            """INSERT INTO golive_channels (site, account_id, enabled, approve_first, cooldown_min, end_action)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(site, account_id) DO UPDATE SET enabled=excluded.enabled,
                 approve_first=excluded.approve_first, cooldown_min=excluded.cooldown_min,
                 end_action=excluded.end_action""",
            (site, account_id, 1 if body.get("enabled") else 0, 0 if body.get("approve_first") is False else 1,
             cooldown, end_action))
        conn.execute("DELETE FROM golive_targets WHERE site = ? AND account_id = ?", (site, account_id))
        conn.executemany(
            "INSERT INTO golive_targets (site, account_id, platform, target_account_id, live_template, ended_template,"
            " allow_mature) VALUES (?, ?, ?, ?, ?, ?, ?)", [(site, account_id, *t) for t in targets])
        conn.commit()
        return {"channel": get_channel(conn, site, account_id), "targets": get_targets(conn, site, account_id)}
    finally:
        conn.close()


# ── Pauses (FR-013) ───────────────────────────────────────────────────────────

def hold_reason(settings: dict, *, auto: bool) -> str:
    """Why nothing may be sent right now, or ''. ``auto`` = posting without the owner's tap."""
    if settings.get("moved_away_at"):
        return "This copy of PawPoller moved to another one, so it doesn't post any more."
    if auto and not settings.get("posting_enabled", False):
        return ("Automatic posting is off (Settings → Posting), so this go-live wasn't sent on its own. "
                "Switch on \"Show me first\" to approve each one instead.")
    return ""


def _site_paused(platform: str) -> bool:
    from polling.site_turns import operator_paused
    return platform in operator_paused()


# ── Signals (the webhook's replay guard) ─────────────────────────────────────

def seen_signal(message_id: str) -> bool:
    """True when this webhook message id was already handled; records it otherwise."""
    conn = get_connection()
    try:
        conn.execute("DELETE FROM golive_signals WHERE received_at < ?", (time.time() - SIGNAL_KEEP_S,))
        cur = conn.execute("INSERT OR IGNORE INTO golive_signals (message_id, received_at) VALUES (?, ?)",
                           (message_id, time.time()))
        conn.commit()
        return cur.rowcount == 0
    finally:
        conn.close()


# ── Streams ───────────────────────────────────────────────────────────────────

async def on_online(site: str, account_id: int, info: dict, detected_by: str = "check") -> int | None:
    """A stream is live. Returns the new run's id, or None (already known, a resume within the
    cool-down, too old to announce, or go-live off)."""
    key = str(info.get("stream_key") or "")
    if not key:
        return None
    conn = get_connection()
    try:
        ch = get_channel(conn, site, account_id)
        if conn.execute("SELECT 1 FROM golive_streams WHERE site = ? AND account_id = ? AND stream_key = ?",
                        (site, account_id, key)).fetchone():
            conn.execute("UPDATE golive_streams SET state = 'live', ended_at = NULL WHERE site = ? AND account_id = ?"
                         " AND stream_key = ? AND state = 'ended'", (site, account_id, key))
            conn.commit()
            return None
        last = conn.execute("SELECT * FROM golive_streams WHERE site = ? AND account_id = ? ORDER BY id DESC LIMIT 1",
                            (site, account_id)).fetchone()
        if last and last["state"] == "ended" and _age_s(last["ended_at"]) <= int(ch["cooldown_min"]) * 60:
            # Dropped and back within the cool-down: Twitch gives the reconnect a new stream id, but to the
            # audience it is the same stream — no second post (FR-011). The new id is remembered on the row.
            conn.execute("UPDATE golive_streams SET state = 'live', ended_at = NULL, stream_key = ? WHERE id = ?",
                         (key, last["id"]))
            conn.commit()
            return None
        if last and last["state"] == "live":
            # A new stream id while the old one still reads live: the old one ended unseen.
            conn.execute("UPDATE golive_streams SET state = 'ended', ended_at = ? WHERE id = ?", (_now(), last["id"]))
        cur = conn.execute(
            "INSERT OR IGNORE INTO golive_streams (site, account_id, stream_key, started_at, title, game, mature, link,"
            " name, detected_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (site, account_id, key, info.get("started_at") or _now(), info.get("title") or "", info.get("game") or "",
             1 if info.get("mature") else 0, info.get("link") or "", info.get("name") or "", detected_by))
        conn.commit()
        if not cur.rowcount:
            return None
        stream_id = cur.lastrowid
        if not ch["enabled"]:
            return None
        if detected_by == "check" and _age_s(info.get("started_at")) > CATCHUP_HOURS * 3600:
            logger.info("go-live: %s stream first seen %.0f h after it started — recorded, not announced",
                        site, _age_s(info.get("started_at")) / 3600)
            return None
        cur = conn.execute("INSERT OR IGNORE INTO golive_runs (stream_id, state, nonce) VALUES (?, 'waiting', ?)",
                           (stream_id, secrets.token_hex(6)))
        conn.commit()
        if not cur.rowcount:
            return None
        run_id = cur.lastrowid
    finally:
        conn.close()

    settings = config.get_settings()
    why = hold_reason(settings, auto=not ch["approve_first"])
    if why:
        _set_run(run_id, "held", reason=why)
        logger.info("go-live: run %s held — %s", run_id, why)
        return run_id
    if ch["approve_first"]:
        await _ask(run_id)
    else:
        await decide(run_id, "post", by="auto")
    return run_id


def on_offline(site: str, account_id: int) -> None:
    """The channel went offline: its live stream ends (finalised once the cool-down passes)."""
    conn = get_connection()
    try:
        conn.execute("UPDATE golive_streams SET state = 'ended', ended_at = ? WHERE site = ? AND account_id = ?"
                     " AND state = 'live'", (_now(), site, account_id))
        conn.commit()
    finally:
        conn.close()


async def on_check_state(site: str, account_id: int, live: dict | None) -> None:
    """A check's answer (the minute check or a stats poll): live with this stream, or not live."""
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO golive_channels (site, account_id, live_state, checked_at) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(site, account_id) DO UPDATE SET live_state = excluded.live_state,"
            " checked_at = excluded.checked_at", (site, account_id, "live" if live else "offline", _now()))
        conn.commit()
    finally:
        conn.close()
    if live:
        await on_online(site, account_id, live, detected_by="check")
    else:
        on_offline(site, account_id)


# ── Runs ──────────────────────────────────────────────────────────────────────

def _set_run(run_id: int, state: str, *, reason: str | None = None, by: str | None = None) -> None:
    conn = get_connection()
    try:
        conn.execute("UPDATE golive_runs SET state = ?, reason = COALESCE(?, reason), decided_by = COALESCE(?, decided_by),"
                     " decided_at = CASE WHEN ? IS NULL THEN decided_at ELSE ? END WHERE id = ?",
                     (state, reason, by, by, _now(), run_id))
        conn.commit()
    finally:
        conn.close()


def get_run(conn, run_id: int) -> dict | None:
    row = conn.execute(
        "SELECT r.*, s.site, s.account_id, s.stream_key, s.title, s.game, s.mature, s.link, s.name, s.started_at,"
        " s.ended_at, s.state AS stream_state FROM golive_runs r JOIN golive_streams s ON s.id = r.stream_id"
        " WHERE r.id = ?", (run_id,)).fetchone()
    if not row:
        return None
    run = dict(row)
    run["targets"] = [dict(r) for r in conn.execute(
        "SELECT * FROM golive_run_targets WHERE run_id = ? ORDER BY id", (run_id,))]
    return run


def list_runs(limit: int = 20) -> list[dict]:
    conn = get_connection()
    try:
        ids = [r[0] for r in conn.execute("SELECT id FROM golive_runs ORDER BY id DESC LIMIT ?", (limit,))]
        return [get_run(conn, i) for i in ids]
    finally:
        conn.close()


def _expired(run: dict) -> bool:
    return run["stream_state"] == "final" or _age_s(run["created_at"]) > EXPIRY_HOURS * 3600


async def decide(run_id: int, action: str, *, by: str, nonce: str | None = None) -> str:
    """Post or Skip a waiting run, once. Returns the run's state afterwards; a run that was not
    waiting (already decided, or lapsed) is left as it was and its state returned."""
    conn = get_connection()
    try:
        run = get_run(conn, run_id)
        if not run or (nonce is not None and not secrets.compare_digest(run["nonce"], nonce)):
            return "missing"
        if run["state"] == "waiting" and _expired(run):
            conn.execute("UPDATE golive_runs SET state = 'expired' WHERE id = ? AND state = 'waiting'", (run_id,))
            conn.commit()
            return "expired"
        new = "posting" if action == "post" else "skipped"
        cur = conn.execute("UPDATE golive_runs SET state = ?, decided_by = ?, decided_at = ? WHERE id = ? AND"
                           " state = 'waiting'", (new, by, _now(), run_id))
        conn.commit()
        if not cur.rowcount:
            return conn.execute("SELECT state FROM golive_runs WHERE id = ?", (run_id,)).fetchone()[0]
    finally:
        conn.close()
    if new == "skipped":
        await _tg_note(run, "Skipped — nothing was posted.")
        return "skipped"
    return await post_run(run_id)


async def post_run(run_id: int) -> str:
    """Send a run's posts, one per target, each recorded with its own result (SC-006)."""
    from database import accounts as accounts_db, posts_queries
    from posting import activity, discord, post_publisher as pp
    settings = config.get_settings()
    conn = get_connection()
    try:
        run = get_run(conn, run_id)
        targets = get_targets(conn, run["site"], run["account_id"]) if run else []
    finally:
        conn.close()
    if not run:
        return "missing"
    why = hold_reason(settings, auto=False)
    if why:
        _set_run(run_id, "held", reason=why)
        return "held"

    never = accounts_db.never_post_ids(settings)
    lines = [(t["platform"], pp.LABELS.get(t["platform"], "Discord" if t["platform"] == "discord" else t["platform"]))
             for t in targets]
    job = activity.start("post", f"Go-live: {run['title'] or SITES.get(run['site'], run['site'])}", lines)
    outcomes = []
    with activity.bound(job):
        for t in targets:
            plat, acct = t["platform"], int(t["target_account_id"] or 0)
            text = render(t["live_template"], run, run["site"])
            res = {"status": "failed", "external_id": "", "external_url": "", "error": "", "post_id": None}
            if acct in never:
                res.update(status="skipped", error=accounts_db.NEVER_POST_ERROR)
            elif _site_paused(plat):
                res.update(status="skipped", error="This site is paused on the box right now.")
            elif run["mature"] and plat in pp._GENERAL_ONLY and not t["allow_mature"]:
                res.update(status="skipped", error=f"The stream is marked mature on {SITES.get(run['site'])}, and "
                                                   f"{pp.LABELS.get(plat, plat)} only takes general posts.")
            elif plat == "discord":
                activity.step("discord", "Sending")
                mid = await discord.send_text(text, settings)
                if mid:
                    res.update(status="posted", external_id=mid)
                else:
                    res["error"] = "Discord didn't take the message — check the announce webhook in Settings."
            else:
                c = get_connection()
                try:
                    post_id = posts_queries.create_post(c, body=text, rating="general", now=_now())
                    c.commit()
                finally:
                    c.close()
                res["post_id"] = post_id
                try:
                    r = (await pp.publish_post(post_id, [plat], {plat: acct}, settings, announce=False))[0]
                except Exception as e:  # noqa: BLE001 — one site's crash is that site's failure
                    r = {"success": False, "error": str(e) or type(e).__name__}
                if r.get("success"):
                    res.update(status="posted", external_id=r.get("external_id", ""),
                               external_url=r.get("external_url", ""))
                else:
                    res["error"] = r.get("error") or "The site refused the post."
            if plat == "discord" or res["status"] == "skipped":
                activity.line_done(plat, res["status"] == "posted", error=res["error"], retryable=False)
            outcomes.append(res["status"])
            c = get_connection()
            try:
                c.execute("INSERT OR REPLACE INTO golive_run_targets (run_id, platform, account_id, post_id, status,"
                          " external_id, external_url, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                          (run_id, plat, acct, res["post_id"], res["status"], res["external_id"],
                           res["external_url"], res["error"][:400]))
                c.commit()
            finally:
                c.close()
    activity.finish(job)
    sent = outcomes.count("posted")
    state = "posted" if sent and sent == len(outcomes) else "partial" if sent else "failed"
    if not outcomes:
        state, why = "failed", "No sites are ticked for go-live posts."
    _set_run(run_id, state, reason=why or None)
    await _tg_note(run, {"posted": f"Posted to {sent} site{'s' if sent != 1 else ''}.",
                         "partial": f"Posted to {sent} of {len(outcomes)} sites — open PawPoller to see which didn't.",
                         "failed": "Nothing could be posted — open PawPoller to see why."}[state])
    logger.info("go-live: run %s %s (%d/%d)", run_id, state, sent, len(outcomes))
    return state


def bell_items() -> list[dict]:
    """For the notification bell: runs waiting for the owner, and recent ones that didn't fully send."""
    out = []
    conn = get_connection()
    try:
        for r in conn.execute(
                "SELECT r.id, r.state, r.reason, r.created_at, s.site, s.title FROM golive_runs r JOIN golive_streams s"
                " ON s.id = r.stream_id WHERE r.state IN ('waiting', 'partial', 'failed', 'held')"
                " AND r.created_at >= datetime('now', '-2 days') ORDER BY r.id DESC LIMIT 10"):
            site = SITES.get(r["site"], r["site"])
            waiting = r["state"] == "waiting"
            out.append({
                "timestamp": r["created_at"], "platform": r["site"], "kind": "golive",
                "status": "warn" if waiting or r["state"] == "held" else "error",
                "summary": (f"You're live on {site} — post your go-live? Open {site} → Go-live posts" if waiting
                            else f"Your go-live wasn't sent: {r['reason']}" if r["state"] == "held"
                            else f"Some go-live posts for your {site} stream didn't send"),
                "detail": r["title"] or None, "url": f"#/{r['site']}",
            })
    finally:
        conn.close()
    return out


# ── Telegram: the Post / Skip message (FR-015) ───────────────────────────────

def _tg():
    from polling.telegram import notification_target
    return notification_target(config.get_settings())


async def _bot_post(token: str, method: str, payload: dict) -> dict | None:
    """One Bot API call; its result, or None. Never raises (the in-app Post / Skip still works)."""
    try:
        async with httpx.AsyncClient(timeout=15.0) as c:
            r = await c.post(f"https://api.telegram.org/bot{token}/{method}", json=payload)
        d = r.json()
        return d.get("result") if d.get("ok") else None
    except Exception as e:  # noqa: BLE001
        logger.warning("go-live: Telegram %s failed: %s", method, type(e).__name__)
        return None


async def _tg_call(method: str, payload: dict) -> dict | None:
    target = _tg()
    return await _bot_post(target[0], method, payload) if target else None


def _esc(s: str) -> str:
    from html import escape
    return escape(s or "", quote=False)


async def _ask(run_id: int) -> None:
    """One message to the owner's private chat with Post / Skip buttons."""
    conn = get_connection()
    try:
        run = get_run(conn, run_id)
        targets = get_targets(conn, run["site"], run["account_id"])
    finally:
        conn.close()
    target = _tg()
    if not target:
        return                                       # the bell and the app's Post / Skip still offer it
    from posting import post_publisher as pp
    from polling.telegram import alert_header
    names = ", ".join(pp.LABELS.get(t["platform"], "Discord" if t["platform"] == "discord" else t["platform"])
                      for t in targets) or "no sites yet"
    text = (f"{alert_header(run['site'], run['account_id'])}\nYou're live: <b>{_esc(run['title'])}</b>"
            + (f" ({_esc(run['game'])})" if run["game"] else "")
            + f"\nPost your go-live to {_esc(names)}?\n<i>This lapses when the stream ends, or in {EXPIRY_HOURS} hours.</i>")
    res = await _tg_call("sendMessage", {
        "chat_id": target[1], "text": text, "parse_mode": "HTML",
        "reply_markup": {"inline_keyboard": [[
            {"text": "Post", "callback_data": f"gl:p:{run_id}:{run['nonce']}"},
            {"text": "Skip", "callback_data": f"gl:s:{run_id}:{run['nonce']}"}]]}})
    if res and res.get("message_id"):
        conn = get_connection()
        try:
            conn.execute("UPDATE golive_runs SET tg_message_id = ? WHERE id = ?", (str(res["message_id"]), run_id))
            conn.commit()
        finally:
            conn.close()


async def _tg_note(run: dict | None, outcome: str) -> None:
    """Swap the Post / Skip message's buttons for what happened."""
    if not run:
        return
    conn = get_connection()
    try:
        mid = (conn.execute("SELECT tg_message_id FROM golive_runs WHERE id = ?", (run["id"],)).fetchone() or [""])[0]
    finally:
        conn.close()
    target = _tg()
    if not (mid and target):
        return
    await _tg_call("editMessageText", {
        "chat_id": target[1], "message_id": int(mid), "parse_mode": "HTML",
        "text": f"You were live: <b>{_esc(run['title'])}</b>\n{_esc(outcome)}"})


async def handle_callback(token: str, chat_id: str, cq: dict) -> bool:
    """A tap on Post / Skip. Only the owner's own private chat counts (FR-015a); anything else is
    answered with nothing done. Returns True when the callback was a go-live one."""
    data = str(cq.get("data") or "")
    if not data.startswith("gl:"):
        return False
    from polling.telegram import is_private_chat
    msg_chat = str(((cq.get("message") or {}).get("chat") or {}).get("id", ""))
    from_id = str((cq.get("from") or {}).get("id", ""))
    note = "That button isn't yours."
    if is_private_chat(chat_id) and msg_chat == str(chat_id) and from_id == str(chat_id):
        parts = data.split(":")
        if len(parts) == 4 and parts[1] in ("p", "s") and parts[2].isdigit():
            state = await decide(int(parts[2]), "post" if parts[1] == "p" else "skip", by="telegram", nonce=parts[3])
            note = {"posted": "Posted.", "partial": "Posted to some sites.", "failed": "Nothing could be posted.",
                    "skipped": "Skipped.", "expired": "That stream's go-live has lapsed.", "held": "Posting is paused.",
                    "missing": "That go-live isn't here any more."}.get(state, f"Already {state}.")
            if state == "expired":
                conn = get_connection()
                try:
                    run = get_run(conn, int(parts[2]))
                finally:
                    conn.close()
                await _tg_note(run, "This go-live lapsed — nothing was posted.")
    await _bot_post(token, "answerCallbackQuery", {"callback_query_id": cq.get("id"), "text": note})
    return True


# ── End of stream (US2) ──────────────────────────────────────────────────────

async def _edit_one(rt: dict, text: str, settings: dict) -> str:
    """Edit one sent post to the ended wording. Returns the edit state."""
    plat, ext = rt["platform"], rt["external_id"]
    if plat not in EDITABLE or not ext:
        return "cant_edit"
    from database import accounts as accounts_db
    if rt["account_id"] in accounts_db.never_post_ids(settings):
        return "cant_edit"
    if plat == "discord":
        from posting import discord
        return "edited" if await discord.edit_text(ext, text, settings) else "edit_failed"
    from posting import post_publisher as pp
    _aid, creds = pp._resolve_creds(plat, rt["account_id"] or None, settings)
    try:
        async with httpx.AsyncClient(timeout=20.0) as c:
            if plat == "mast":
                from clients.mast.client import _normalise_instance
                base = _normalise_instance(creds.get("mast_instance_url", ""))
                r = await c.put(f"{base}/api/v1/statuses/{ext}", data={"status": text},
                                headers={"Authorization": f"Bearer {creds.get('mast_access_token', '')}"})
                return "edited" if r.status_code == 200 else "edit_failed"
            from clients.tg.client import TgClient
            channel = TgClient(bot_token=creds.get("tg_bot_token", ""), channel=creds.get("tg_channel", "")).channel
            r = await c.post(f"https://api.telegram.org/bot{creds.get('tg_bot_token', '')}/editMessageText",
                             json={"chat_id": channel, "message_id": int(ext), "text": text})
            return "edited" if (r.json() or {}).get("ok") else "edit_failed"
    except Exception as e:  # noqa: BLE001
        logger.warning("go-live: end-of-stream edit on %s failed: %s", plat, type(e).__name__)
        return "edit_failed"


async def finalise() -> None:
    """Streams whose cool-down has passed are over: a waiting run lapses; with "edit when it ends",
    each sent post becomes the ended wording where the site lets PawPoller edit it."""
    settings = config.get_settings()
    conn = get_connection()
    try:
        due = [dict(r) for r in conn.execute(
            "SELECT s.*, c.cooldown_min, c.end_action FROM golive_streams s LEFT JOIN golive_channels c"
            " ON c.site = s.site AND c.account_id = s.account_id WHERE s.state = 'ended'")
            if _age_s(r["ended_at"]) > int(r["cooldown_min"] if r["cooldown_min"] is not None else 15) * 60]
        for s in due:
            conn.execute("UPDATE golive_streams SET state = 'final' WHERE id = ?", (s["id"],))
        # A waiting run lapses with its stream, or after the expiry window.
        lapsed = [r[0] for r in conn.execute(
            "SELECT r.id FROM golive_runs r JOIN golive_streams s ON s.id = r.stream_id WHERE r.state = 'waiting'"
            " AND (s.state = 'final' OR r.created_at < datetime('now', ?))", (f"-{EXPIRY_HOURS} hours",))]
        for rid in lapsed:
            conn.execute("UPDATE golive_runs SET state = 'expired' WHERE id = ? AND state = 'waiting'", (rid,))
        conn.commit()
        runs = [get_run(conn, r[0]) for r in conn.execute(
            "SELECT id FROM golive_runs WHERE stream_id IN (%s)" % ",".join("?" * len(due)),
            [s["id"] for s in due])] if due else []
        lapsed_runs = [get_run(conn, rid) for rid in lapsed]
    finally:
        conn.close()
    for run in lapsed_runs:
        await _tg_note(run, "This go-live lapsed — nothing was posted.")
    ends = {s["id"]: s["end_action"] for s in due}
    for run in runs:
        if ends.get(run["stream_id"]) != "edit":
            continue
        conn = get_connection()
        try:
            templates = {(t["platform"], t["target_account_id"]): t["ended_template"]
                         for t in get_targets(conn, run["site"], run["account_id"])}
        finally:
            conn.close()
        for rt in run["targets"]:
            if rt["status"] != "posted" or rt["edit_state"]:
                continue
            text = render(templates.get((rt["platform"], rt["account_id"])) or DEFAULT_ENDED, run, run["site"])
            state = await _edit_one(rt, text, settings)
            conn = get_connection()
            try:
                conn.execute("UPDATE golive_run_targets SET edit_state = ? WHERE id = ?", (state, rt["id"]))
                conn.commit()
            finally:
                conn.close()


# ── The loop: live check, webhook subscriptions, expiry, end of stream ───────

def webhook_base() -> str:
    """The public https address Twitch can push to, or '' (desktop / no tunnel)."""
    import os
    base = (os.environ.get("PAWPOLLER_PUBLIC_BASE_URL") or "").strip().rstrip("/")
    return base if base.startswith("https://") else ""


def eventsub_secret() -> str:
    key = config.get_settings().get("ttv_eventsub_secret") or ""
    if not key:
        key = secrets.token_hex(24)
        config.save_settings({"ttv_eventsub_secret": key})
    return key


def _watched() -> list[dict]:
    """Twitch accounts with go-live on: {account_id, creds, channel row}."""
    from database import accounts as accounts_db
    from polling.ttv_poller import account_creds
    conn = get_connection()
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT c.* FROM golive_channels c JOIN accounts a ON a.account_id = c.account_id"
            " WHERE c.site = 'ttv' AND c.enabled = 1 AND a.enabled = 1 AND a.platform = 'ttv'")]
    finally:
        conn.close()
    out = []
    for ch in rows:
        creds, _ = account_creds(ch["account_id"])
        if creds.get("ttv_user_id"):
            out.append({"account_id": ch["account_id"], "creds": creds, "channel": ch})
    return out


async def check_live(watched: list[dict] | None = None, *, force: bool = False) -> None:
    """One request per Twitch app for every channel that's due."""
    from polling.ttv_poller import make_client
    watched = _watched() if watched is None else watched
    now = time.time()
    by_app: dict[str, list[dict]] = {}
    for w in watched:
        every = CHECK_EVERY_WITH_WEBHOOK_S if w["channel"].get("sub_state") == "ok" else CHECK_EVERY_S
        if force or now - _last_check.get(("ttv", w["account_id"]), 0) >= every - 5:
            by_app.setdefault(make_client(w["creds"]).client_id, []).append(w)
    for group in by_app.values():
        client = make_client(group[0]["creds"])
        try:
            live = await client.get_streams([w["creds"]["ttv_user_id"] for w in group])
        except Exception as e:  # noqa: BLE001 — the next tick tries again
            logger.warning("go-live: Twitch live check failed: %s", e)
            continue
        finally:
            await client.close()
        for w in group:
            _last_check[("ttv", w["account_id"])] = now
            await on_check_state("ttv", w["account_id"], live.get(w["creds"]["ttv_user_id"]))


async def reconcile_webhooks(watched: list[dict] | None = None) -> None:
    """Keep Twitch's go-live subscriptions matching the watched channels (seats only)."""
    global _last_reconcile
    base = webhook_base()
    if not base or time.time() - _last_reconcile < RECONCILE_EVERY_S:
        return
    _last_reconcile = time.time()
    from polling.ttv_poller import make_client
    watched = _watched() if watched is None else watched
    callback = f"{base}/hooks/twitch"
    if not watched:
        return
    client = make_client(watched[0]["creds"])
    try:
        subs = [s for s in await client.list_subscriptions() if (s.get("transport") or {}).get("callback") == callback]
        have = {((s.get("condition") or {}).get("broadcaster_user_id"), s.get("type")): s for s in subs}
        want = {w["creds"]["ttv_user_id"]: w for w in watched}
        for k, s in list(have.items()):      # ours for an unwatched channel, or one Twitch gave up on
            if k[0] not in want or s.get("status") not in _LIVE_SUB:
                await client.unsubscribe(s["id"])
                del have[k]
        secret = eventsub_secret()
        for uid, w in want.items():
            ok = True
            for kind in ("stream.online", "stream.offline"):
                s = have.get((uid, kind))
                if s:
                    continue
                try:
                    await client.subscribe(kind, uid, callback, secret)
                except Exception as e:  # noqa: BLE001
                    ok = False
                    logger.warning("go-live: couldn't subscribe to %s: %s", kind, e)
            conn = get_connection()
            try:
                conn.execute("UPDATE golive_channels SET sub_state = ?, sub_checked_at = ? WHERE site = 'ttv' AND"
                             " account_id = ?", ("ok" if ok else "failed", time.time(), w["account_id"]))
                conn.commit()
            finally:
                conn.close()
    except Exception as e:  # noqa: BLE001
        logger.warning("go-live: webhook subscriptions not checked: %s", e)
    finally:
        await client.close()


def mark_revoked(user_id: str) -> None:
    """Twitch dropped a subscription: the minute check takes over until the next reconcile."""
    global _last_reconcile
    aid = account_for_user("ttv", user_id)
    if aid is None:
        return
    conn = get_connection()
    try:
        conn.execute("UPDATE golive_channels SET sub_state = 'revoked' WHERE site = 'ttv' AND account_id = ?", (aid,))
        conn.commit()
    finally:
        conn.close()
    _last_reconcile = 0.0


def account_for_user(site: str, user_id: str) -> int | None:
    """The watched account whose channel has this Twitch id (go-live on)."""
    for w in _watched():
        if str(w["creds"].get("ttv_user_id")) == str(user_id):
            return w["account_id"]
    return None


async def tick() -> None:
    settings = config.get_settings()
    if settings.get("moved_away_at"):
        return
    from polling.desktop_pollers import is_paused
    watched = _watched()
    if watched and not is_paused(settings, "ttv"):
        await check_live(watched)
        await reconcile_webhooks(watched)
    await finalise()


async def run_loop() -> None:
    """Every minute while PawPoller runs, on the install that owns polling (a connected desktop
    leaves it to its server, as the Telegram bot does)."""
    from posting.scheduler import detect_runtime_mode
    logger.info("Go-live watcher started")
    while True:
        try:
            if config.get_polling_owner(detect_runtime_mode()) == "local":
                await tick()
        except Exception:
            logger.warning("go-live tick failed", exc_info=True)
        await asyncio.sleep(CHECK_EVERY_S)


def preview(site: str, account_id: int) -> list[dict]:
    """The posts a pretend stream would make — nothing is sent."""
    sample = {"title": "Sketching commissions", "game": "Art", "link": "https://www.twitch.tv/yourchannel",
              "name": "YourChannel"}
    conn = get_connection()
    try:
        return [{"platform": t["platform"], "account_id": t["target_account_id"],
                 "live": render(t["live_template"], sample, site), "ended": render(t["ended_template"], sample, site)}
                for t in get_targets(conn, site, account_id)]
    finally:
        conn.close()
