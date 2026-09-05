"""REST API endpoints for the analytics dashboard.

This is the primary Inkbunny (IB) routes module. It handles:
  - Authentication (login/logout with credential cascade)
  - Polling controls (trigger, full-resync, progress)
  - Submission data retrieval (list, detail, snapshots, comparison)
  - CSV export via DictWriter -> StreamingResponse
  - Group CRUD (create, read, update, delete groups and members)
  - Analytics (top fans, trending submissions)
  - Cross-platform link management (link submissions across IB/FA/WS)
  - Auto-update (check for new versions, download and apply)
  - Thumbnail proxy (CORS bypass for Inkbunny CDN images)
  - Telegram notification setup (bot token, chat_id discovery)
  - User preferences (poll intervals, notification filters)
  - Settings management (credentials, preferences, Telegram config)
"""

from __future__ import annotations
import asyncio
import csv
import io
import json
import logging
import sqlite3
import shutil
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Query, HTTPException, UploadFile, File
from fastapi.responses import Response, StreamingResponse

from database.db import get_connection, init_db
from database import (
    queries, fa_queries, ws_queries, sf_queries, sqw_queries, ao3_queries,
    da_queries, wp_queries, ik_queries, bsky_queries, tw_queries, mast_queries, tum_queries, pix_queries, thr_queries, ig_queries,
    e621_queries, fn_queries, fbr_queries, tg_queries,
    group_queries, analytics_queries, platform_metrics,
    accounts as accounts_db,
)
from polling.poller import run_poll_cycle, poll_progress
from polling.background import spawn, spawn_poll
from clients.ib.client import InkbunnyClient
import config
import updater

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api")


@router.get("/health")
async def health_check():
    """Lightweight health check for Docker HEALTHCHECK and monitoring.

    Returns 200 with {"status": "ok", "version": "..."} if the web
    server is responsive. Does not check individual poller health —
    that would add latency and coupling. The point is to detect a
    completely dead container.

    2.16.8: added `version` so monitoring/CI can confirm a deploy
    actually rolled out without parsing the dashboard HTML.
    """
    return {"status": "ok", "version": config.APP_VERSION}


# In-memory credentials for "don't remember me" logins.
# When the user logs in without ticking "remember me", credentials are stored
# here in the process memory rather than persisted to settings.json on disk.
# They survive for the lifetime of the server process but are lost on restart.
# Protected by _cred_lock to prevent race conditions between the web server
# thread (writes) and poller threads (reads).
import threading
_session_credentials: dict = {}
_cred_lock = threading.Lock()


def get_effective_credentials() -> tuple[str, str]:
    """Return (username, password) using a three-tier credential cascade.

    The cascade checks sources in priority order:
      1. Session memory (_session_credentials) -- set by "don't remember me" logins
      2. settings.json on disk -- set by "remember me" logins or the settings page
      3. config module globals (INKBUNNY_USERNAME / INKBUNNY_PASSWORD) -- loaded at
         startup from environment variables or .env file

    This allows temporary logins to override persisted credentials, and persisted
    credentials to override the initial config defaults.
    """
    with _cred_lock:
        if _session_credentials.get("username") and _session_credentials.get("password"):
            return _session_credentials["username"], _session_credentials["password"]
    settings = config.get_settings()
    username = settings.get("username") or config.INKBUNNY_USERNAME
    password = settings.get("password") or config.INKBUNNY_PASSWORD
    return username, password


# Long-lived httpx client for proxying thumbnail requests.
# Reused across requests to benefit from connection pooling.
_thumb_client = httpx.AsyncClient(timeout=15.0)


# ── Authentication ────────────────────────────────────────────
# Inkbunny uses username/password authentication. The login flow validates
# credentials against the real Inkbunny API before accepting them locally.
# Passwords are NEVER returned in any API response -- only a boolean
# "has_password" flag is exposed via GET /settings/credentials.

@router.get("/auth/status")
def auth_status():
    """Check whether credentials exist and whether there is any data yet.

    Used by the frontend on initial load to decide whether to show the
    login page or the main dashboard. Checks the credential cascade for
    any available username/password, and queries the DB for submission count.
    """
    username, password = get_effective_credentials()
    has_credentials = bool(username and password)
    has_data = False
    conn = get_connection()
    try:
        count = conn.execute("SELECT COUNT(*) as c FROM submissions").fetchone()["c"]
        has_data = count > 0
    except Exception:
        pass
    finally:
        conn.close()
    return {"has_credentials": has_credentials, "has_data": has_data}


@router.post("/auth/login")
async def auth_login(body: dict):
    """Validate credentials against the real Inkbunny API; optionally persist them.

    Auth flow:
      1. Receive username + password from the frontend
      2. Create a temporary InkbunnyClient and attempt login against the live API
      3. If the API rejects (wrong password, banned, etc.), parse the error and
         return a 401 with the Inkbunny-provided error message
      4. On success, hot-reload the config globals so the background poller
         immediately picks up the new credentials without a server restart
      5. If "remember" is true, persist to settings.json on disk
         If "remember" is false, store in _session_credentials (in-memory only)
    """
    username = body.get("username", "").strip()
    password = body.get("password", "").strip()
    remember = body.get("remember", False)

    if not username or not password:
        raise HTTPException(400, "Username and password are required")

    # Test login against the live Inkbunny API to validate credentials
    from polling.cf_proxy import proxy_kwargs
    client = InkbunnyClient(username=username, password=password,
                            **proxy_kwargs(config.get_settings(), "ib"))
    try:
        await client.login()
    except Exception as e:
        # Extract a clean error message from the Inkbunny API response dict.
        # The raw exception string contains the full dict repr; we pull out
        # just the human-readable 'error_message' value for the frontend.
        err_str = str(e)
        if "error_message" in err_str:
            import re
            match = re.search(r"'error_message':\s*'([^']+)'", err_str)
            if match:
                err_str = match.group(1)
        raise HTTPException(401, detail=err_str)
    finally:
        await client.close()

    # Hot-reload: update the config module globals in-place so the background
    # poller uses these new credentials on its next cycle, without needing
    # a full server restart. Lock ensures poller reads consistent username+password.
    with _cred_lock:
        config.INKBUNNY_USERNAME = username
        config.INKBUNNY_PASSWORD = password

        if remember:
            # Persist to settings.json so credentials survive server restarts
            config.save_settings({"username": username, "password": password})
        else:
            # Store in process memory only -- lost on restart
            _session_credentials["username"] = username
            _session_credentials["password"] = password

    return {"status": "success", "message": "Authenticated successfully"}


@router.post("/auth/logout")
def auth_logout():
    """Clear all credentials from every tier of the cascade and reset state.

    Clears:
      1. In-memory session credentials
      2. Config module globals (prevents poller from re-using old creds)
      3. settings.json on disk (removes persisted username/password)
      4. Cached API session in the database (forces full re-auth on next poll)
    """
    with _cred_lock:
        _session_credentials.clear()
        config.INKBUNNY_USERNAME = ""
        config.INKBUNNY_PASSWORD = ""
    # Remove from settings.json on disk
    config.delete_settings_keys(["username", "password"])
    # Clear the cached Inkbunny API session (SID) from the database so the
    # next poll cycle will perform a fresh login rather than reusing a stale SID
    conn = get_connection()
    try:
        queries.clear_session(conn)
    except Exception:
        pass
    finally:
        conn.close()
    return {"status": "success", "message": "Logged out"}


# ── Poll Controls ─────────────────────────────────────────────
# Two polling actions are available:
#   - poll/trigger: Normal incremental poll -- fetches only new/changed data
#   - poll/full-resync: Forces a complete re-scrape of all faves, comments,
#     and submission details regardless of whether changes were detected.
#     Useful when data appears out of sync or after a schema migration.

@router.get("/poll/progress")
def get_poll_progress():
    """Return the current poll progress state.

    The poll_progress dict is updated in real-time by the poller module
    during a poll cycle. It contains fields like current step, total steps,
    and a human-readable message for the frontend progress bar.
    """
    return dict(poll_progress)


def _normalize_progress(prog) -> dict:
    """One shape for the UI ticker: ``{active, phase, current, total, message}``.

    Telegram's poller exports ``{"running": …, "platform": "tg"}`` instead —
    it counts subscribers rather than walking a gallery. Normalising here
    keeps that difference out of the poller, which is where it belongs.
    """
    d = dict(prog)
    if "active" not in d:
        d["active"] = bool(d.get("running"))
    return d


@router.get("/poll/all-progress")
def get_all_poll_progress():
    """Return progress state for every platform in one call.

    2.16.9: collapses what used to be 9 simultaneous fetches into one.
    The frontend ticker fires every 10s (idle) / 1.5s (active), so the
    fan-out spammed 9× errors into DevTools whenever the session
    cookie blipped. Each value is a dict with `active`, `phase`,
    `current`, `total`, `message` — the same shape every per-platform
    /api/{p}/poll/progress already returns.

    Imports are local so a missing poller module (e.g. partial deploy)
    can't take the whole endpoint down — that platform's slot just
    becomes None. Per-platform endpoints stay alive for direct callers
    and backwards compatibility.

    4.3.2: derived from ``multi_account.get_poll_progress()`` rather than
    hand-listed. The old list stopped at ``e621``, so FurryNetwork, Furbooru
    and Telegram had no slot at all and the UI's progress strip could not show
    them — the same stopping point as four other hand-written lists.
    """
    from polling.multi_account import get_poll_progress

    progress = {}
    try:
        registry = get_poll_progress()
    except Exception as e:                    # a partial deploy, as before
        logger.debug("all-progress: registry unavailable: %s", e)
        return {"ib": _normalize_progress(poll_progress)}
    for code, prog in registry.items():
        try:
            progress[code] = _normalize_progress(prog)
        except Exception as e:
            logger.debug("all-progress: %s unreadable: %s", code, e)
            progress[code] = None
    return progress


# Per-platform health endpoint config: (code, queries module, last-poll
# fn, interval setting key, "is configured" predicate). Drives the
# /api/platforms/health endpoint that powers sidebar status dots,
# platform-header "last polled · next in" subtitles, and throttle
# banners — one fetch instead of fanning out 11 × poll_log requests.
_PLATFORM_HEALTH_CONFIG = [
    ("ib",   queries,      "get_last_poll",      "poll_interval_minutes",      lambda s: bool(s.get("username") and s.get("password"))),
    ("fa",   fa_queries,   "get_fa_last_poll",   "fa_poll_interval_minutes",   lambda s: bool(s.get("fa_cookie_a") and s.get("fa_cookie_b"))),
    ("ws",   ws_queries,   "get_ws_last_poll",   "ws_poll_interval_minutes",   lambda s: bool(s.get("ws_api_key"))),
    ("sf",   sf_queries,   "get_sf_last_poll",   "sf_poll_interval_minutes",   lambda s: bool(s.get("sf_api_token"))),
    ("sqw",  sqw_queries,  "get_sqw_last_poll",  "sqw_poll_interval_minutes",  lambda s: bool(s.get("sqw_username") and s.get("sqw_password"))),
    # AO3 accepts username+password OR session cookie (mirrors server.py:213 gate)
    ("ao3",  ao3_queries,  "get_ao3_last_poll",  "ao3_poll_interval_minutes",  lambda s: bool((s.get("ao3_username") and s.get("ao3_password")) or s.get("ao3_session_cookie"))),
    # DA accepts OAuth app creds (the real path since 2.47.0) OR the legacy
    # cookie — matching da_poller's own gate. Requiring the cookie made an
    # OAuth-only install report DeviantArt as unconfigured on the status table.
    ("da",   da_queries,   "get_da_last_poll",   "da_poll_interval_minutes",   lambda s: bool(s.get("da_target_user") and ((s.get("da_client_id") and s.get("da_client_secret")) or s.get("da_cookie")))),
    ("wp",   wp_queries,   "get_wp_last_poll",   "wp_poll_interval_minutes",   lambda s: bool(s.get("wp_target_user"))),
    ("ik",   ik_queries,   "get_ik_last_poll",   "ik_poll_interval_minutes",   lambda s: bool(s.get("ik_target_user"))),
    ("bsky", bsky_queries, "get_bsky_last_poll", "bsky_poll_interval_minutes", lambda s: bool(s.get("bsky_identifier") and s.get("bsky_app_password"))),
    ("tw",   tw_queries,   "get_tw_last_poll",   "tw_poll_interval_minutes",   lambda s: bool(s.get("tw_auth_token") and s.get("tw_ct0"))),
    ("mast", mast_queries, "get_mast_last_poll", "mast_poll_interval_minutes", lambda s: bool(s.get("mast_instance_url") and s.get("mast_access_token"))),
    ("tum", tum_queries, "get_tum_last_poll", "tum_poll_interval_minutes", lambda s: bool(s.get("tum_api_key") and s.get("tum_blog"))),
    ("pix", pix_queries, "get_pix_last_poll", "pix_poll_interval_minutes", lambda s: bool(s.get("pix_refresh_token"))),
    ("thr", thr_queries, "get_thr_last_poll", "thr_poll_interval_minutes", lambda s: bool(s.get("thr_access_token"))),
    ("ig", ig_queries, "get_ig_last_poll", "ig_poll_interval_minutes", lambda s: bool(s.get("ig_access_token"))),
    ("e621", e621_queries, "get_e621_last_poll", "e621_poll_interval_minutes", lambda s: bool(s.get("e621_username") and s.get("e621_api_key"))),
    # fn and fbr have had poll logs since they shipped but were never listed
    # here, so both polled every cycle with no status dot and no "next in".
    # Telegram's cycle fetches a subscriber count and nothing else, so its log
    # rows carry submissions_found=0 by design.
    #
    # These three take their "configured" predicate from the accounts registry
    # rather than restating it. The entries above still carry their own copies,
    # and that duplication has already gone wrong twice: DeviantArt's copy
    # demanded the legacy cookie after OAuth became the real path, and
    # FurryNetwork is currently described three different ways across three
    # files. A new platform should not add a fourth place to keep in sync.
    ("fn",  fn_queries,  "get_fn_last_poll",  "fn_poll_interval_minutes",  accounts_db.DEFAULT_CRED_CHECKS["fn"]),
    ("fbr", fbr_queries, "get_fbr_last_poll", "fbr_poll_interval_minutes", accounts_db.DEFAULT_CRED_CHECKS["fbr"]),
    ("tg",  tg_queries,  "get_tg_last_poll",  "tg_poll_interval_minutes",  accounts_db.DEFAULT_CRED_CHECKS["tg"]),
]


@router.get("/platforms/health")
def get_platforms_health():
    """Per-platform health snapshot.

    Single endpoint that backs the sidebar status dot, the per-platform
    page-header "last polled · next in" subtitle, and the throttle
    banner that surfaces AO3's per-IP cooldown. Frontend polls this on
    a 60s interval; it's cheap (one DB connection, 11 indexed lookups
    against {p}_poll_log + a settings dict read) so a single fetch
    fans out across every status surface that needs the same data.

    Per-platform shape:
        configured       : bool   — credentials present
        last_poll_at     : ISO datetime | None
        last_poll_status : 'success' | 'error' | 'running' | None
        last_poll_error  : str | None
        interval_minutes : int    — configured poll interval
        next_poll_at     : ISO datetime | None — last_poll_at + interval
        throttled_until  : ISO datetime | None — currently AO3-only
                           (sourced from ao3 client backoff cache)
    """
    from datetime import datetime, timedelta, timezone
    from polling.session_check import get_session_health
    settings = config.get_settings()
    sessions = get_session_health()
    fallback_interval = int(settings.get("poll_interval_minutes", 60))
    out: dict = {}
    conn = get_connection()
    try:
        for code, qmodule, fn_name, interval_key, configured_fn in _PLATFORM_HEALTH_CONFIG:
            entry = {
                "configured": False,
                "last_poll_at": None,
                "last_poll_status": None,
                "last_poll_error": None,
                "interval_minutes": int(settings.get(interval_key, fallback_interval)),
                "next_poll_at": None,
                "throttled_until": None,
            }
            try:
                entry["configured"] = bool(configured_fn(settings))
                last = getattr(qmodule, fn_name)(conn)
                if last:
                    started = last.get("started_at")
                    entry["last_poll_at"] = started
                    entry["last_poll_status"] = last.get("status")
                    if last.get("status") in ("error", "partial"):
                        entry["last_poll_error"] = last.get("error_message")
                    if started:
                        # SQLite stores started_at via datetime('now') as
                        # naive UTC; tag it explicitly so the frontend's
                        # Date parser doesn't drift by the local offset.
                        try:
                            dt = datetime.fromisoformat(started.replace("Z", "+00:00"))
                            if dt.tzinfo is None:
                                dt = dt.replace(tzinfo=timezone.utc)
                            entry["next_poll_at"] = (
                                dt + timedelta(minutes=entry["interval_minutes"])
                            ).isoformat()
                        except ValueError:
                            pass
            except Exception as e:
                logger.debug("platforms/health: %s lookup failed: %s", code, e)
            # Active session-validity (from the periodic session-check cache):
            # {status: valid|expired|error|unconfigured, detail, checked_at} or
            # None for platforms with no standalone validate_session() probe.
            entry["session"] = sessions.get(code)
            out[code] = entry

        # AO3 throttle bolt-on. Module-level state in
        # clients.ao3.client._ao3_backoff_until_ts is process-local and
        # rebuilt by the first 429 each cycle; reading it here is safe
        # even if the import fails (e.g. partial deploy) because the
        # whole probe is wrapped.
        try:
            from clients.ao3.client import get_backoff_until_ts
            until_ts = get_backoff_until_ts()
            if until_ts and until_ts > datetime.now(timezone.utc).timestamp():
                out["ao3"]["throttled_until"] = (
                    datetime.fromtimestamp(until_ts, timezone.utc).isoformat()
                )
        except Exception as e:
            logger.debug("platforms/health: ao3 throttle probe failed: %s", e)
    finally:
        conn.close()
    return out


@router.get("/platforms/sessions")
def get_platform_sessions():
    """Cached active-session-validity snapshot for the credential platforms.

    Shape per platform code: {status, detail, checked_at} where status is
    'valid' | 'expired' | 'error' | 'unconfigured'. Empty until the first
    check runs (startup + every ~6h; see polling/session_check.py). The
    banner + Settings dots read this; the same data is folded into
    /platforms/health so the 60s health poll surfaces it without a 2nd fetch.
    """
    from polling.session_check import get_session_health, CHECKABLE
    return {"sessions": get_session_health(), "checkable": list(CHECKABLE)}


@router.get("/platforms/credential-age")
def get_credential_age():
    """Proactive credential-age report (backlog W): warns before a finite-lifetime
    cookie/token login (X/FA/DA) goes stale, rather than after it fails.

    Returns ``{report: [{code, set_at, age_days, ttl_days, level}], warnings: [...]}``
    where ``warnings`` is the aging/stale subset the UI surfaces. Backfills a
    'now' stamp for any configured-but-unstamped tracked platform on first call
    so tracking starts immediately on existing installs."""
    config.backfill_credential_stamps()
    report = config.credential_age_report()
    warnings = [r for r in report if r["level"] in ("aging", "stale")]
    return {"report": report, "warnings": warnings}


@router.post("/platforms/sessions/check")
async def trigger_session_check():
    """Force an immediate re-validation of every configured session.

    validate_session() makes real network calls (8 platforms, serial), so we
    fire-and-forget via spawn() and return immediately. The frontend re-fetches
    /platforms/sessions a few seconds later to pick up the fresh results.

    Deliberately plain ``spawn``, not ``spawn_poll``: this only reads session
    validity and writes no analytics, so it stays available on a paired desktop
    where the manual poll triggers do not.
    """
    from polling.session_check import check_all
    from polling.background import spawn
    spawn(check_all(), "manual-session-check")
    return {"status": "started"}


@router.post("/platforms/sessions/mute")
def mute_session_alert(body: dict):
    """Mute (or unmute) a platform's session-health alert.

    A muted alert stays visible in the notification feed but stops popping a
    toast and stops counting toward the unread badge — the "quiet, I already
    know" state for a problem the user is handling externally (e.g. a Meta
    app-block they're fixing in the Meta dashboard). It is NOT a fix: the mute
    auto-clears the next time that platform's session validates (see
    polling/session_check.check_platform), so a genuinely new failure later
    re-alerts. Body: {code, muted}. Only the checkable (session-validated)
    platforms have a mutable alert.
    """
    from polling.session_check import CHECKABLE
    code = str(body.get("code", "")).strip().lower()
    if code not in CHECKABLE:
        raise HTTPException(400, f"'{code}' has no mutable session alert")
    muted = bool(body.get("muted", True))
    cur = list(config.get_settings().get("muted_session_codes", []) or [])
    if muted and code not in cur:
        cur.append(code)
    elif not muted:
        cur = [c for c in cur if c != code]
    config.save_settings({"muted_session_codes": cur})
    return {"status": "success", "muted_session_codes": cur}


def _format_poll_summary(log: dict) -> str:
    """Compose a single-line summary like '+2 faves, +1 comment' from
    a poll_log row's delta counters. Returns 'no changes' when nothing
    new came back, or 'failed' on error. Used by /api/activity/recent."""
    if log.get("status") == "error":
        return "poll failed"
    if log.get("status") == "running":
        return "poll in progress"
    parts = []
    for counter, label in [
        ("new_faves_found", "fave"),
        ("new_comments_found", "comment"),
        ("new_watchers_found", "watcher"),
    ]:
        n = log.get(counter) or 0
        if n:
            parts.append(f"+{n} {label}{'s' if n != 1 else ''}")
    if not parts:
        subs = log.get("submissions_found") or 0
        return f"no changes ({subs} subs scanned)" if subs else "no changes"
    return ", ".join(parts)


def _format_post_summary(log: dict) -> str:
    """Single-line summary for a posting_log row."""
    action = log.get("action", "post")
    story = log.get("story_name", "?")
    chapter = log.get("chapter_index") or 0
    chap_suffix = f" ch{chapter}" if chapter else ""
    if log.get("status") == "error":
        return f"{action} failed: {story}{chap_suffix}"
    return f"{action} {story}{chap_suffix}"


def _collect_activity_events(limit: int = 30) -> list:
    """Unified system-event timeline across all 11 platforms + posting.

    Merges every platform's most-recent poll_log entries with recent
    posting_log entries into one chronological feed. Backs both the Overview
    page's "Recent System Events" panel and the notification centre.

    Each entry shape:
        timestamp : ISO datetime — when the event happened
        platform  : platform code ('ib','fa','ws',…) or 'posting'
        kind      : 'poll' | 'post' | 'edit' | 'update' | …
        status    : 'success' | 'error' | 'running' | 'partial' | …
        summary   : short single-line description for the feed line
        detail    : longer detail (error message) or None
    """
    polls: list[dict] = []
    posts: list[dict] = []
    per_platform = max(1, limit // 4)
    conn = get_connection()
    try:
        for code, qmodule, _last_fn, _interval, _configured in _PLATFORM_HEALTH_CONFIG:
            log_fn_name = "get_poll_log" if code == "ib" else f"get_{code}_poll_log"
            try:
                logs = getattr(qmodule, log_fn_name)(conn, per_platform)
                for log in logs:
                    polls.append({
                        "timestamp": log.get("started_at"),
                        "platform": code,
                        "kind": "poll",
                        "status": log.get("status"),
                        "summary": _format_poll_summary(log),
                        "detail": log.get("error_message"),
                    })
            except Exception as e:
                logger.debug("activity/recent: %s poll_log failed: %s", code, e)

        # Posting events are wrapped separately so a missing posting
        # schema (older deployments) doesn't break the rest of the feed.
        try:
            from database.posting_queries import get_posting_log
            # content_type=None is load-bearing: `get_posting_log` DEFAULTS to
            # "story" so the Stories log view never shows artwork, which meant
            # this cross-cutting feed silently excluded every artwork post ever
            # made. A piece could go to nine sites and the timeline would show
            # nothing but polling — the larger half of "the activity window
            # isn't recording stuff like you'd expect" (3.17.0).
            for log in get_posting_log(conn, story_name=None, limit=limit,
                                       content_type=None):
                posts.append({
                    "timestamp": log.get("created_at"),
                    "platform": log.get("platform") or "posting",
                    "kind": log.get("action") or "post",
                    "status": log.get("status"),
                    "summary": _format_post_summary(log),
                    "detail": log.get("error_message"),
                })
        except Exception as e:
            logger.debug("activity/recent: posting_log skipped: %s", e)
    finally:
        conn.close()

    # Posting keeps a reserved share of the feed (3.17.0).
    #
    # A poll CYCLE fires all 11 platforms at once, so it lands ~11 rows sharing
    # a timestamp. Merging everything and taking the newest `limit` therefore
    # let two poll cycles fill a 30-row feed outright and push that afternoon's
    # posts off the bottom — the feed said "nothing has been posted" at exactly
    # the moment someone had just posted and wanted to see it.
    #
    # Polling is high-volume and low-interest; posting is the opposite. So the
    # newest posting events are seated FIRST and polls fill what is left, rather
    # than the two competing on timestamp alone. Nothing is invented: if there
    # are no posting events, polls take the whole feed as before.
    _by_time = lambda e: e.get("timestamp") or ""      # noqa: E731
    polls.sort(key=_by_time, reverse=True)
    posts.sort(key=_by_time, reverse=True)

    reserved = min(len(posts), max(1, limit // 3))
    seated = posts[:reserved]
    rest = sorted(polls + posts[reserved:], key=_by_time, reverse=True)
    merged = seated + rest[:max(0, limit - len(seated))]
    merged.sort(key=_by_time, reverse=True)
    return merged


@router.get("/activity/recent")
def get_recent_activity(limit: int = 30):
    """Thin wrapper over _collect_activity_events for the Overview panel."""
    return {"events": _collect_activity_events(limit), "limit": limit}


def _norm_ts(s) -> str:
    """Normalise a timestamp for string comparison. Poll/posting rows store
    naive-UTC 'YYYY-MM-DD HH:MM:SS'; session + last_read use UTC isoformat with
    a 'T' and offset. Collapse both to 'YYYY-MM-DD HH:MM:SS' so unread math
    (a plain string >) is correct across the two formats."""
    return str(s or "").replace("T", " ")[:19]


@router.get("/notifications")
def get_notifications(limit: int = 40):
    """Notification-centre feed for the bell dropdown.

    Merges recent poll + posting events with synthetic session-expiry events
    from the session-check cache, newest first, and flags each item plus a
    total 'unread' count against the server-side 'notifications_last_read_at'
    marker (so unread state follows the account across devices). The bell polls
    this for the badge and renders the list on open; opening the dropdown POSTs
    /notifications/mark-read to clear the badge.
    """
    items = _collect_activity_events(limit)
    try:
        from polling.session_check import summarize_problems, get_session_health
        sess = get_session_health()
        muted_codes = set(config.get_settings().get("muted_session_codes", []) or [])
        for prob in summarize_problems(sess):
            entry = sess.get(prob["code"]) or {}
            expired = prob["status"] == "expired"
            items.append({
                "timestamp": entry.get("checked_at"),
                "platform": prob["code"],
                "kind": "session",
                # A muted alert stays in the feed but goes quiet (no toast, no
                # unread) — the frontend renders an Unmute control for it.
                "muted": prob["code"] in muted_codes,
                "status": "error" if expired else "warn",
                "summary": (f"{prob['label']} session expired" if expired
                            else f"{prob['label']} session could not be verified"),
                "detail": prob.get("detail"),
            })
    except Exception as e:
        logger.debug("notifications: session events skipped: %s", e)

    # Mirror drift (3.18.0): the background watcher found the server has work
    # this install does not. Synthetic, like the session events above — there
    # is no notifications table, the feed is assembled from live state.
    #
    # ⚠ Deliberately phrased as an offer, not a warning. Nothing is wrong when
    # a paired desktop is behind; the server is simply where the work happened.
    # It also never syncs on its own — see mirror/watcher.py for why that stays
    # a decision the person makes.
    try:
        from mirror.watcher import STATE as _drift
        if _drift.get("in_sync") is False and _drift.get("checked_at"):
            n = _drift.get("files_to_fetch") or 0
            items.append({
                "timestamp": _drift["checked_at"],
                "platform": "mirror",
                "kind": "sync",
                "status": "info",
                "summary": (f"{n} item(s) on the server are newer than this copy"
                            if n else "The server has changes this copy does not"),
                "detail": "Open Settings → Sync with server to bring them down.",
            })
    except Exception as e:  # noqa: BLE001
        logger.debug("notifications: mirror drift skipped: %s", e)

    # Poll-throttle events: a platform whose LAST poll was 'partial' (rate-limited
    # or blocked, e.g. X's 429) — so a throttled cycle isn't a silent "success".
    # Deduped by the poll's started_at, so it's one alert per throttled cycle.
    try:
        from polling.session_check import LABELS as _HLABELS
        _tsettings = config.get_settings()
        _tconn = get_connection()
        try:
            for _code, _qmod, _fn, _ik, _cfg in _PLATFORM_HEALTH_CONFIG:
                try:
                    if not _cfg(_tsettings):   # skip platforms with no credentials
                        continue
                    _last = getattr(_qmod, _fn)(_tconn)
                except Exception:
                    continue
                if _last and _last.get("status") == "partial":
                    items.append({
                        "timestamp": _last.get("started_at"),
                        "platform": _code,
                        "kind": "throttle",
                        "status": "warn",
                        "summary": f"{_HLABELS.get(_code, _code.upper())}: last poll was throttled",
                        "detail": _last.get("error_message")
                        or "Rate-limited — some data may be incomplete. It fills in on the next un-throttled poll.",
                    })
        finally:
            _tconn.close()
    except Exception as e:
        logger.debug("notifications: throttle events skipped: %s", e)

    items.sort(key=lambda e: _norm_ts(e.get("timestamp")), reverse=True)

    settings = config.get_settings()
    # A "Clear" persists a watermark; drop anything at or before it. The feed is
    # rebuilt each poll from the activity logs, so this is how a clear survives a
    # refresh without deleting rows. Applied before the limit so cleared items
    # don't consume slots.
    cleared_at = _norm_ts(settings.get("notifications_cleared_at"))
    if cleared_at:
        items = [it for it in items if _norm_ts(it.get("timestamp")) > cleared_at]
    items = items[:limit]

    last_read = _norm_ts(settings.get("notifications_last_read_at"))
    unread = 0
    for it in items:
        # Muted session alerts never count toward the unread badge.
        it["unread"] = (not it.get("muted")) and _norm_ts(it.get("timestamp")) > last_read
        if it["unread"]:
            unread += 1
    return {"items": items, "unread": unread, "last_read_at": last_read}


@router.post("/notifications/mark-read")
def mark_notifications_read():
    """Mark everything up to now as read — clears the bell's unread badge."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    config.save_settings({"notifications_last_read_at": now})
    return {"ok": True, "last_read_at": now}


@router.post("/notifications/clear")
def clear_notifications():
    """Clear the feed — hide every event up to now and reset the unread badge.

    The feed is rebuilt each poll from the activity logs, so 'clearing' can't
    delete rows; instead we persist a 'notifications_cleared_at' watermark and
    get_notifications drops anything at or before it. A still-broken session
    resurfaces after the next session check (its checked_at moves past the
    watermark) — a persistent problem should re-nag; a transient one stays gone.
    """
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    config.save_settings({
        "notifications_cleared_at": now,
        "notifications_last_read_at": now,
    })
    return {"ok": True, "cleared_at": now}


@router.get("/status")
def get_status():
    """Polling status, last/next poll time, total submissions."""
    conn = get_connection()
    try:
        last_poll = queries.get_last_poll(conn)
        count = conn.execute("SELECT COUNT(*) as c FROM submissions").fetchone()["c"]
        snap_count = conn.execute("SELECT COUNT(*) as c FROM snapshots").fetchone()["c"]
        return {
            "total_submissions": count,
            "total_snapshots": snap_count,
            "last_poll": last_poll,
        }
    except Exception as e:
        logger.error("Error in /api/status: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@router.get("/summary")
def get_summary(account_id: int | None = Query(None)):
    """Dashboard summary: totals, top 5, fastest growing, recent faves, growth rates.

    With *account_id* set, the totals / top-lists / recent activity are scoped to
    that account ("All accounts" by default). growth_rates + watcher counts stay
    aggregate for now (a Phase 2 follow-up).
    """
    conn = get_connection()
    try:
        summary = queries.get_summary(conn, account_id=account_id)
        summary["growth_rates"] = queries.get_growth_rates(conn)
        summary["total_watchers"] = queries.get_watchers_count(conn)
        summary["recent_watchers"] = queries.get_recent_watchers(conn, limit=10)
        return summary
    except Exception as e:
        logger.error("Error in /api/summary: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


# ── Submission Data ───────────────────────────────────────────

@router.get("/submissions")
def get_submissions(
    sort_by: str = Query("views", description="Sort field"),
    order: str = Query("desc", description="Sort order"),
    search: str = Query("", description="Search title/keywords"),
    rating: str = Query("", description="Filter by rating"),
    type_name: str = Query("", description="Filter by type"),
    account_id: int | None = Query(None),
):
    """All submissions with latest stats, sortable/filterable.

    Fetches all submissions from the database, then applies in-memory filtering
    for search text, rating, and type. Deltas (change since last poll) are
    merged in from a separate query so the frontend can show +/- indicators.
    """
    conn = get_connection()
    try:
        subs = queries.get_all_submissions(conn, sort_by=sort_by, order=order, account_id=account_id)
        # Get per-submission deltas (views/faves/comments change since last poll)
        deltas = queries.get_submission_deltas(conn)

        # In-memory filtering -- applied after DB fetch because the query module
        # handles sorting but not arbitrary text/rating/type filtering
        if search:
            search_lower = search.lower()
            subs = [s for s in subs if search_lower in s["title"].lower() or search_lower in (s.get("keywords") or "").lower()]
        if rating:
            subs = [s for s in subs if str(s.get("rating_id")) == rating or s.get("rating_name", "").lower() == rating.lower()]
        if type_name:
            subs = [s for s in subs if s.get("type_name", "").lower() == type_name.lower()]

        # Merge delta values into each submission dict for the frontend
        for s in subs:
            d = deltas.get(s["submission_id"], {})
            s["views_delta"] = d.get("views_delta", 0)
            s["faves_delta"] = d.get("faves_delta", 0)
            s["comments_delta"] = d.get("comments_delta", 0)

        return {"submissions": subs, "total": len(subs)}
    except Exception as e:
        logger.error("Error in /api/submissions: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@router.get("/submissions/{submission_id}")
def get_submission(submission_id: int):
    """Full detail + snapshot history + faving users + comments + growth rates.

    Returns the complete picture for a single submission detail page:
    the submission metadata, all historical snapshots (for charting),
    the list of users who faved it, all comments, and per-metric growth rates.
    """
    conn = get_connection()
    try:
        sub = queries.get_submission(conn, submission_id)
        if not sub:
            raise HTTPException(status_code=404, detail="Submission not found")
        snapshots = queries.get_snapshots(conn, submission_id)
        faving = queries.get_faving_users(conn, submission_id)
        comments = queries.get_comments(conn, submission_id)
        growth_rates = queries.get_submission_growth_rates(conn, submission_id)
        tags = _get_submission_tags(conn, "ib", submission_id)
        sub_dict = dict(sub) if not isinstance(sub, dict) else sub
        sub_dict["tags"] = tags
        return {"submission": sub_dict, "snapshots": snapshots, "faving_users": faving, "comments": comments, "growth_rates": growth_rates}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error in /api/submissions/%d: %s", submission_id, e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@router.get("/submissions/{submission_id}/snapshots")
def get_submission_snapshots(
    submission_id: int,
    start: Optional[str] = Query(None, description="Start datetime"),
    end: Optional[str] = Query(None, description="End datetime"),
):
    """Time-series data for a single submission, with optional date range filtering."""
    conn = get_connection()
    try:
        return {"snapshots": queries.get_snapshots(conn, submission_id, start, end)}
    except Exception as e:
        logger.error("Error in /api/submissions/%d/snapshots: %s", submission_id, e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@router.get("/aggregate")
def get_aggregate(
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
    account_id: int | None = Query(None),
):
    """Aggregate time-series across all submissions.

    Sums views/faves/comments across every submission at each poll timestamp,
    providing a single combined time-series for the "all submissions" chart.
    With *account_id* set, the totals are scoped to that account.
    """
    conn = get_connection()
    try:
        return {"snapshots": queries.get_aggregate_snapshots(conn, start, end, account_id=account_id)}
    except Exception as e:
        logger.error("Error in /api/aggregate: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@router.get("/comparison")
def get_comparison(
    ids: str = Query(..., description="Comma-separated submission IDs"),
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
):
    """Multi-submission time-series for overlay charts.

    Accepts up to 10 comma-separated submission IDs and returns per-submission
    snapshot series keyed by ID, plus a titles map for chart legends.
    Capped at 10 to keep response sizes reasonable for the frontend chart library.
    """
    try:
        submission_ids = [int(x.strip()) for x in ids.split(",") if x.strip()]
    except ValueError:
        raise HTTPException(400, "Invalid submission IDs")
    if len(submission_ids) > 10:
        raise HTTPException(400, "Max 10 submissions for comparison")

    conn = get_connection()
    try:
        data = queries.get_comparison_snapshots(conn, submission_ids, start, end)
        titles = {}
        for sid in submission_ids:
            sub = queries.get_submission(conn, sid)
            if sub:
                titles[sid] = sub["title"]
        # Convert int keys to string keys for JSON serialisation compatibility
        return {"series": {str(k): v for k, v in data.items()}, "titles": {str(k): v for k, v in titles.items()}}
    except Exception as e:
        logger.error("Error in /api/comparison: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@router.get("/watchers")
def get_watchers():
    """Recent watchers list with total count."""
    conn = get_connection()
    try:
        watchers = queries.get_recent_watchers(conn, limit=50)
        count = queries.get_watchers_count(conn)
        return {"watchers": watchers, "total": count}
    finally:
        conn.close()


@router.get("/poll_log")
def get_poll_log(limit: int = Query(50, ge=1, le=200)):
    """Recent poll history -- shows timestamps, durations, and results of past polls."""
    conn = get_connection()
    try:
        return {"polls": queries.get_poll_log(conn, limit)}
    except Exception as e:
        logger.error("Error in /api/poll_log: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


@router.post("/poll/trigger")
async def trigger_poll():
    """Manual 'refresh now' -- runs an incremental poll cycle inline.

    This is a normal poll: it checks for new submissions and updated stats,
    but only re-scrapes faves/comments for submissions whose counts changed.
    Compare with /poll/full-resync which forces a complete re-scrape of everything.
    """
    try:
        spawn_poll(run_poll_cycle(), "run_poll_cycle")
        return {"status": "started"}
    # Let an explicit HTTPException through — the ownership guard in
    # spawn_poll raises 409 here, and the blanket handler below would
    # otherwise report it as a 500 'internal error'.
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error in /api/poll/trigger: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))


@router.post("/poll/trigger/{code}")
async def trigger_account_poll(code: str, account_id: int | None = Query(None)):
    """Manual poll for one platform, optionally scoped to a single account.

    ``account_id`` given → poll just that account; omitted → poll EVERY enabled
    account for the platform (not only the default). Backs the account picker on
    the dashboard poll button. Manual polls are explicit, so they ignore the
    scheduled-cycle round-robin/save-tokens throttle — you get exactly what you
    asked for.
    """
    from polling.multi_account import poll_platform_accounts, get_poll_cycles
    if code not in get_poll_cycles():
        raise HTTPException(404, f"Unknown platform: {code}")
    try:
        scope = account_id if account_id is not None else "all"
        spawn_poll(poll_platform_accounts(code, account_id),
              f"poll_platform_accounts:{code}:{scope}")
        return {"status": "started", "platform": code, "account_id": account_id}
    # Let an explicit HTTPException through — the ownership guard in
    # spawn_poll raises 409 here, and the blanket handler below would
    # otherwise report it as a 500 'internal error'.
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error in /api/poll/trigger/%s: %s", code, e, exc_info=True)
        raise HTTPException(500, detail=str(e))


@router.post("/poll/full-resync")
async def full_resync():
    """Force full resync -- re-scrapes ALL faves and comments regardless of changes.

    Unlike poll/trigger which only fetches fave/comment details for submissions
    whose counts changed, this forces a complete re-scrape of every submission's
    faving users and comments. Useful for:
      - Recovering from data inconsistencies
      - After schema migrations that add new tracked fields
      - When faving user lists appear incomplete
    """
    try:
        spawn_poll(run_poll_cycle(force_full=True), "run_poll_cycle full-resync")
        return {"status": "started"}
    # Let an explicit HTTPException through — the ownership guard in
    # spawn_poll raises 409 here, and the blanket handler below would
    # otherwise report it as a 500 'internal error'.
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error in /api/poll/full-resync: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))


@router.post("/poll/pause")
async def pause_polling():
    """Pause all scheduled background polling across all platforms.

    Sets the polling_paused flag in settings.json.  Poller loops check this
    flag each cycle and skip when paused.  Manual Poll Now still works.
    Sends a Telegram notification.
    """
    config.save_settings({"polling_paused": True})
    logger.info("Polling PAUSED by user")
    try:
        from polling.telegram import send_telegram
        await send_telegram("⏸ <b>Polling paused</b>\nAll scheduled background polls are now skipped.\nManual polls still work.")
    except Exception:
        pass
    return {"status": "success", "polling_paused": True}


@router.post("/poll/resume")
async def resume_polling():
    """Resume scheduled background polling across all platforms."""
    config.save_settings({"polling_paused": False})
    logger.info("Polling RESUMED by user")
    try:
        from polling.telegram import send_telegram
        await send_telegram("▶️ <b>Polling resumed</b>\nScheduled background polls will run on their normal intervals.")
    except Exception:
        pass
    return {"status": "success", "polling_paused": False}


@router.get("/poll/paused")
def get_poll_paused():
    """Return current polling pause state (global + per-platform)."""
    settings = config.get_settings()
    return {
        "polling_paused": settings.get("polling_paused", False),
        "paused_platforms": settings.get("polling_paused_platforms", []) or [],
    }


# Valid platform codes for the per-platform pause toggle — every platform the
# orchestrator schedules. Derived rather than hand-listed: the old literal
# stopped at e621, so pausing FurryNetwork, Furbooru or Telegram returned
# 400 "Unknown platform" for platforms that were being polled every cycle.
_PAUSEABLE_PLATFORMS = frozenset(platform_metrics.ALL_CODES)


@router.post("/poll/pause/{code}")
def pause_platform_polling(code: str):
    """Pause scheduled polling for ONE platform (leaves the others running).

    Adds the code to polling_paused_platforms; the orchestrator's _poll_all
    skips paused codes each cycle. Manual Poll Now / Full Resync still work.
    """
    code = code.lower()
    if code not in _PAUSEABLE_PLATFORMS:
        raise HTTPException(400, f"Unknown platform: {code}")
    settings = config.get_settings()
    paused = set(settings.get("polling_paused_platforms", []) or [])
    paused.add(code)
    config.save_settings({"polling_paused_platforms": sorted(paused)})
    logger.info("Polling PAUSED for %s by user", code)
    return {"status": "success", "code": code, "paused_platforms": sorted(paused)}


@router.post("/poll/resume/{code}")
def resume_platform_polling(code: str):
    """Resume scheduled polling for ONE previously-paused platform."""
    code = code.lower()
    if code not in _PAUSEABLE_PLATFORMS:
        raise HTTPException(400, f"Unknown platform: {code}")
    settings = config.get_settings()
    paused = set(settings.get("polling_paused_platforms", []) or [])
    paused.discard(code)
    config.save_settings({"polling_paused_platforms": sorted(paused)})
    logger.info("Polling RESUMED for %s by user", code)
    return {"status": "success", "code": code, "paused_platforms": sorted(paused)}


@router.post("/session/clear")
def clear_session():
    """Clear the cached Inkbunny API session (SID) from the database.

    Forces a fresh login on the next poll cycle. Useful when the session
    has expired or become invalid (e.g., after a password change on Inkbunny).
    """
    conn = get_connection()
    try:
        queries.clear_session(conn)
        return {"status": "success", "message": "Session cleared — next poll will re-authenticate"}
    except Exception as e:
        logger.error("Error in /api/session/clear: %s", e, exc_info=True)
        raise HTTPException(500, detail=str(e))
    finally:
        conn.close()


# ── Settings: Credentials ────────────────────────────────────
# Security note: passwords are NEVER returned in API responses.
# GET /settings/credentials returns the username and a boolean "has_password"
# flag only, so the frontend can show whether a password is saved without
# ever exposing the actual password value.

@router.get("/settings/credentials")
def get_credentials():
    """Return saved username and whether a password exists (never the password itself).

    This endpoint deliberately omits the password value for security.
    The frontend uses "has_password" to show a placeholder in the password
    field and to know whether the user needs to re-enter it.
    """
    settings = config.get_settings()
    return {
        "username": settings.get("username", ""),
        "has_password": bool(settings.get("password")),
    }


@router.post("/settings/credentials")
def save_credentials(body: dict):
    """Save Inkbunny credentials to settings.json and hot-reload config globals.

    If only the username is provided (no password), the existing password in
    settings.json is preserved. This allows the frontend to update the username
    without requiring the user to re-enter their password.
    """
    username = body.get("username", "").strip()
    password = body.get("password", "").strip()
    if not username:
        raise HTTPException(400, "Username is required")

    # Only include password in the update if one was actually provided,
    # so we don't accidentally blank out an existing saved password
    update = {"username": username}
    if password:
        update["password"] = password

    config.save_settings(update)

    # Hot-reload: update config module globals so the poller uses new
    # credentials immediately without requiring a server restart
    config.INKBUNNY_USERNAME = username
    if password:
        config.INKBUNNY_PASSWORD = password

    return {"status": "success", "message": "Credentials saved"}


# ── Settings: Preferences ────────────────────────────────────
# Preferences control application behaviour like poll intervals, notification
# filters, and system tray/startup settings. Each preference is individually
# optional in the request body -- only provided fields are updated.

@router.get("/settings/preferences")
def get_preferences():
    """Return all application preferences with sensible defaults.

    Covers every user-configurable preference across all 11 platforms:
      - notifications_enabled / {platform}_ : master toggle per platform
      - poll_interval_minutes / {platform}_ : how often to poll (from allowed set)
      - notification_comments_only / {platform}_ : only notify on new comments
      - watcher_notifications_enabled / fa_ : toggle watcher alerts per platform
      - notification_min_faves_delta : minimum new-fave count to trigger notification
      - notification_min_views_delta : stored for future use (no view-based notifications yet)
      - display_timezone : timezone for Telegram messages and UI timestamps
      - milestone_* : threshold arrays for Telegram milestone alerts
    """
    settings = config.get_settings()
    return {
        # ── Application ────────────────────────────────────────────
        "minimize_to_tray": settings.get("minimize_to_tray", False),
        "auto_update": settings.get("auto_update", True),
        "update_skip_version": settings.get("update_skip_version", ""),
        "run_on_startup": config.get_run_on_startup(),
        "display_timezone": settings.get("display_timezone", "UTC"),
        "theme": settings.get("theme", "dark"),
        "mobile_mode": settings.get("mobile_mode", "auto"),
        "auto_sync_enabled": settings.get("auto_sync_enabled", True),
        "fa_direct_polling": settings.get("fa_direct_polling", False),
        # Floating "Logs" button (bottom-right live-tail widget). Default on;
        # users who don't want the debug control can hide it from Settings.
        "logs_panel_enabled": settings.get("logs_panel_enabled", True),
        # Throttle X polling (round-robin) to save paid API reads. Only bites
        # when the official API backend is active; scrapers round-robin anyway.
        "tw_roundrobin_save_tokens": settings.get("tw_roundrobin_save_tokens", False),
        # ── Configurable Home dashboard widget layout (redesign) ───
        # Free-form list of {id, span} objects; null until the user
        # customises (the frontend falls back to its default layout).
        "dashboard_layout": settings.get("dashboard_layout", None),
        # ── Guided-tour "seen" set (2.82.0) ────────────────────────
        # List of tour names the user has completed/dismissed. Backs the
        # onboarding tours server-side so a dismissal sticks across Safari,
        # the installed PWA, the desktop app and updates — localStorage was
        # per-origin/per-browser and would re-show tours on a fresh store.
        "tours_seen": settings.get("tours_seen", []),
        # ── Per-platform notification master toggles ───────────────
        # Registry-derived so a new platform cannot be rendered by the settings
        # UI while being invisible here — see platform_metrics.setting_keys.
        **{k: settings.get(k, True)
           for k in platform_metrics.setting_keys("notifications_enabled")},
        # ── Watcher / follower notification toggles ────────────────
        "watcher_notifications_enabled": settings.get("watcher_notifications_enabled", True),
        "fa_watcher_notifications_enabled": settings.get("fa_watcher_notifications_enabled", True),
        # ── Per-platform poll intervals (minutes) ──────────────────
        **{k: settings.get(k, 60)
           for k in platform_metrics.setting_keys("poll_interval_minutes")},
        # ── Notification filter preferences ────────────────────────
        # When enabled, notifications are only sent for new comments
        # (suppressing fave/activity alerts for that platform).
        "notification_comments_only": settings.get("notification_comments_only", False),
        "fa_notification_comments_only": settings.get("fa_notification_comments_only", False),
        "ws_notification_comments_only": settings.get("ws_notification_comments_only", False),
        "sf_notification_comments_only": settings.get("sf_notification_comments_only", False),
        # Minimum delta thresholds: fave notifications are suppressed unless
        # the new-fave count in a cycle meets or exceeds this value.
        # notification_min_views_delta is stored but not yet consumed -- no
        # platform currently generates view-change-based notifications.
        "notification_min_views_delta": settings.get("notification_min_views_delta", 0),
        "notification_min_faves_delta": settings.get("notification_min_faves_delta", 0),
        # ── Milestone thresholds (Telegram) ────────────────────────
        "milestone_views": settings.get("milestone_views", [100, 250, 500, 1000, 2500, 5000, 10000, 25000, 50000, 100000]),
        "milestone_faves": settings.get("milestone_faves", [10, 25, 50, 100, 250, 500, 1000, 2500, 5000]),
        "milestone_comments": settings.get("milestone_comments", [10, 25, 50, 100, 250, 500, 1000]),
        # ── Per-platform CF Proxy backup toggles (2.18.6) ──────────
        # Only the eight platforms that don't *require* the proxy.
        # AO3 / DA / SF use it implicitly when cf_worker_url is set.
        "ib_use_cf_proxy":   settings.get("ib_use_cf_proxy", False),
        "fa_use_cf_proxy":   settings.get("fa_use_cf_proxy", False),
        "ws_use_cf_proxy":   settings.get("ws_use_cf_proxy", False),
        "sqw_use_cf_proxy":  settings.get("sqw_use_cf_proxy", False),
        "bsky_use_cf_proxy": settings.get("bsky_use_cf_proxy", False),
        "ik_use_cf_proxy":   settings.get("ik_use_cf_proxy", False),
        "wp_use_cf_proxy":   settings.get("wp_use_cf_proxy", False),
        "tw_use_cf_proxy":   settings.get("tw_use_cf_proxy", False),
        "mast_use_cf_proxy": settings.get("mast_use_cf_proxy", False),
        "tum_use_cf_proxy":  settings.get("tum_use_cf_proxy", False),
        "pix_use_cf_proxy":  settings.get("pix_use_cf_proxy", False),
        "thr_use_cf_proxy":  settings.get("thr_use_cf_proxy", False),
        "ig_use_cf_proxy":   settings.get("ig_use_cf_proxy", False),
        # Whether the worker URL/key are configured at all (drives the
        # disabled state on the UI toggles).
        "cf_worker_configured": bool(settings.get("cf_worker_url")) and bool(settings.get("cf_worker_key")),
    }


@router.post("/settings/preferences")
def save_preferences(body: dict):
    """Save application preferences and apply startup registry change.

    Each field is individually optional -- only provided keys are updated.
    Special handling:
      - run_on_startup: modifies the Windows registry (or equivalent) via config
      - *_poll_interval_minutes: validated against the allowed set
        {15, 30, 60, 120, 240, 360, 480, 600, 720} to prevent abuse or
        unreasonably fast polling that could get the user rate-limited by
        platform APIs. Invalid values are silently ignored.
    """
    update = {}

    # ── Application toggles ────────────────────────────────────
    if "minimize_to_tray" in body:
        update["minimize_to_tray"] = bool(body["minimize_to_tray"])
    # Startup update gate (4.9.0): on by default; one version may be skipped.
    if "auto_update" in body:
        update["auto_update"] = bool(body["auto_update"])
    if "update_skip_version" in body:
        update["update_skip_version"] = str(body.get("update_skip_version") or "").strip()[:32]
    if "telegram_enabled" in body:
        update["telegram_enabled"] = bool(body["telegram_enabled"])
    if "auto_sync_enabled" in body:
        update["auto_sync_enabled"] = bool(body["auto_sync_enabled"])
    # FA direct-polling: when true, skip the (currently Cloudflare-blocked)
    # FAExport proxy and scrape FurAffinity directly via cookies. Only works
    # from a residential IP (the desktop instance), not the datacenter server.
    if "fa_direct_polling" in body:
        update["fa_direct_polling"] = bool(body["fa_direct_polling"])
    # Floating "Logs" button visibility (frontend-only preference).
    if "logs_panel_enabled" in body:
        update["logs_panel_enabled"] = bool(body["logs_panel_enabled"])
    # Throttle X polling to save paid API reads (round-robin under the official API).
    if "tw_roundrobin_save_tokens" in body:
        update["tw_roundrobin_save_tokens"] = bool(body["tw_roundrobin_save_tokens"])
    # Theme — accepted as opaque string; client-side validates against the
    # THEMES catalogue so unknown ids never reach here. Whitelist anyway as
    # belt-and-braces against rogue clients.
    if "theme" in body:
        theme_val = str(body["theme"])
        if theme_val in {"dark", "light", "ink_copper", "parchment",
                         "midnight_press", "forest", "velvet", "high_contrast",
                         "retro_2005"}:
            update["theme"] = theme_val
    # Mobile UX override. `auto` (default) follows viewport via matchMedia;
    # `on` forces the mobile layout on every screen size; `off` suppresses
    # the new mobile-mode-only enhancements (existing media queries still
    # fire on small viewports). Whitelisted to a known set.
    if "mobile_mode" in body:
        mm_val = str(body["mobile_mode"])
        if mm_val in {"auto", "on", "off"}:
            update["mobile_mode"] = mm_val

    # ── Per-platform notification master toggles ───────────────
    # Each platform poller checks its own *_notifications_enabled flag
    # before sending Windows toasts or Telegram alerts.
    # Registry-derived for the same reason as the intervals above — the old
    # hand-list stopped at e621, so fn/fbr/tg toggles saved nothing.
    for key in platform_metrics.setting_keys("notifications_enabled"):
        if key in body:
            update[key] = bool(body[key])

    # ── Watcher / follower notification toggles ────────────────
    # Separate from the master toggle so users can get submission alerts
    # without watcher alerts (or vice versa).
    if "watcher_notifications_enabled" in body:
        update["watcher_notifications_enabled"] = bool(body["watcher_notifications_enabled"])
    if "fa_watcher_notifications_enabled" in body:
        update["fa_watcher_notifications_enabled"] = bool(body["fa_watcher_notifications_enabled"])

    # ── Notification filter preferences ────────────────────────
    # When enabled, suppress fave/activity notifications and only alert
    # on new comments.  Each platform's poller applies its own filter.
    for key in (
        "notification_comments_only",     # IB
        "fa_notification_comments_only",
        "ws_notification_comments_only",
        "sf_notification_comments_only",
    ):
        if key in body:
            update[key] = bool(body[key])

    # Minimum delta thresholds: fave notifications are suppressed unless
    # the new-fave count in a cycle meets or exceeds this value.
    if "notification_min_views_delta" in body:
        update["notification_min_views_delta"] = max(0, int(body["notification_min_views_delta"]))
    if "notification_min_faves_delta" in body:
        update["notification_min_faves_delta"] = max(0, int(body["notification_min_faves_delta"]))

    # ── Per-platform poll intervals ────────────────────────────
    # The allowed set balances data freshness against API rate limits.
    # Values outside this set are silently rejected to prevent
    # misconfiguration. NOTE: this MUST stay in sync with the option
    # values the settings dropdowns render (app.js Poll Intervals) —
    # any value offered there but missing here saves nothing (the
    # 6/8/10/12-hour options were dropped this way before 2.99.0).
    _ALLOWED_INTERVALS = (15, 30, 60, 120, 240, 360, 480, 600, 720)
    # Derived from the metrics registry rather than hand-listed: the hand-list
    # stopped at e621, so fn, fbr and tg were rendered by the settings UI and
    # silently dropped on save. See platform_metrics.setting_keys.
    for key in platform_metrics.setting_keys("poll_interval_minutes"):
        if key in body:
            val = int(body[key])
            if val in _ALLOWED_INTERVALS:
                update[key] = val

    # ── Timezone ───────────────────────────────────────────────
    if "display_timezone" in body:
        update["display_timezone"] = str(body["display_timezone"])

    # ── Milestone threshold arrays ─────────────────────────────
    # Validate as sorted positive integer lists
    for ms_key in ("milestone_views", "milestone_faves", "milestone_comments"):
        if ms_key in body:
            try:
                vals = sorted(int(v) for v in body[ms_key] if int(v) > 0)
                if vals:
                    update[ms_key] = vals
            except (TypeError, ValueError):
                pass

    # ── Per-platform CF Proxy backup toggles ───────────────────
    for key in (
        "ib_use_cf_proxy", "fa_use_cf_proxy", "ws_use_cf_proxy",
        "sqw_use_cf_proxy", "bsky_use_cf_proxy", "ik_use_cf_proxy",
        "wp_use_cf_proxy", "tw_use_cf_proxy", "mast_use_cf_proxy", "tum_use_cf_proxy", "pix_use_cf_proxy", "thr_use_cf_proxy",
        "ig_use_cf_proxy",
    ):
        if key in body:
            update[key] = bool(body[key])

    # ── Configurable Home dashboard layout (redesign) ──────────
    # Free-form JSON list of {id, span, cfg} widget descriptors. Stored
    # verbatim so the Home dashboard layout follows the user across
    # devices (desktop + phone share one settings store). `cfg` carries
    # per-widget options — the charts widget's line/bar choice and each
    # widget's `exclude` list of platform codes it should NOT count.
    if "dashboard_layout" in body:
        update["dashboard_layout"] = body["dashboard_layout"]

    # ── Windows startup registry ───────────────────────────────
    # Handled separately because it modifies the system registry
    # (Windows) or launch agents (macOS) rather than settings.json
    if "run_on_startup" in body:
        enabled = bool(body["run_on_startup"])
        config.set_run_on_startup(enabled)
    if update:
        config.save_settings(update)
    return {"status": "success", "message": "Preferences saved"}


# ── Settings: guided-tour "seen" set (2.82.0) ─────────────────
# The onboarding tours (frontend/js/tour.js) used to record "seen" only in
# per-browser localStorage, so a dismissal didn't follow the user across
# Safari, the installed PWA, the desktop app, or a device change — the tour
# would re-offer on any fresh store. This endpoint persists the seen set in
# settings.json instead.
#
# It is ADDITIVE by design: it appends one tour name to the stored list and
# never removes anything, so it is race-safe across concurrent tabs and a
# rogue/partial client can't wipe a user's whole seen set. The frontend reads
# the full list back from GET /settings/preferences (tours_seen) at startup.

@router.post("/settings/tour-seen")
def mark_tour_seen(body: dict):
    """Record that a guided tour has been completed/dismissed.

    Body: {"name": "<tour-name>"} — e.g. "getting-started" or a page name
    like "platforms". Unknown/empty names are rejected. Returns the updated
    full seen list so the client can reconcile its in-memory set.
    """
    name = str(body.get("name", "")).strip()
    if not name or len(name) > 64:
        raise HTTPException(400, "A tour name is required")
    settings = config.get_settings()
    seen = settings.get("tours_seen", [])
    if not isinstance(seen, list):
        seen = []
    if name not in seen:
        seen = seen + [name]
        config.save_settings({"tours_seen": seen})
    return {"status": "success", "tours_seen": seen}


# ── Settings: Telegram ────────────────────────────────────────
# Telegram setup flow:
#   1. User creates a bot via @BotFather and gets a bot token
#   2. User sends /start to their bot on Telegram
#   3. Frontend POSTs the bot token to /settings/telegram
#   4. Backend calls Telegram's getUpdates API to find the chat_id
#      from the /start message the user sent
#   5. Both bot_token and chat_id are saved to settings.json
#   6. Notifications can now be sent via the bot to that chat
#
# The token is never fully exposed via GET -- only a boolean "token_set" flag.

@router.get("/settings/telegram")
def get_telegram():
    """Return Telegram connection status (never expose the full token).

    Returns boolean flags so the frontend can show connection state without
    leaking sensitive credentials. The full bot token is never sent to the client.
    """
    settings = config.get_settings()
    token = settings.get("telegram_bot_token", "")
    chat_id = settings.get("telegram_chat_id", "")
    from polling.telegram import is_private_chat
    return {
        "token_set": bool(token),
        "chat_id_set": bool(chat_id),
        "enabled": settings.get("telegram_enabled", False),
        "connected": bool(token and chat_id),
        # False = the saved chat is a channel/group: nothing is sent there (4.8.0)
        "chat_is_private": is_private_chat(chat_id) if chat_id else True,
    }


@router.post("/settings/telegram")
async def connect_telegram(body: dict):
    """Accept bot token, call getUpdates to auto-discover the chat_id, save both.

    The setup flow works by leveraging Telegram's getUpdates endpoint:
      1. Validate the bot token by calling the Telegram API
      2. If the token is invalid, Telegram returns ok=false and we reject it
      3. Iterate through recent updates (messages) looking for any chat object
         -- this finds the /start message the user sent to the bot
      4. Extract the chat_id from that message
      5. If no messages found, the user hasn't sent /start yet -- return a
         helpful error message telling them to do so
      6. Save token + chat_id + enabled flag to settings.json
    """
    bot_token = body.get("bot_token", "").strip()
    if not bot_token:
        raise HTTPException(400, "Bot token is required")
    _existing = config.get_settings()
    if bot_token == (_existing.get("tg_bot_token") or "").strip():
        raise HTTPException(400, "That is your channel-posting bot. Notifications need their own bot "
                                 "(4.8.0) — make another one in @BotFather and paste that token here.")

    # Call Telegram's getUpdates to validate the token and find the chat_id
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"https://api.telegram.org/bot{bot_token}/getUpdates")
            data = resp.json()
    except Exception as e:
        raise HTTPException(502, f"Failed to contact Telegram API: {e}")

    if not data.get("ok"):
        raise HTTPException(400, "Invalid bot token — Telegram rejected it")

    # Search through recent updates for any message containing a chat object.
    # This finds the /start message (or any message) the user sent to the bot,
    # which gives us the chat_id needed to send notifications back to them.
    # We also check my_chat_member events which are generated when the user
    # first interacts with the bot.
    # ⚠ Only a PRIVATE chat will do (4.8.0). A bot that administers a channel
    # receives `my_chat_member` and `channel_post` updates carrying the CHANNEL's
    # chat, and before 4.8.0 this scan took the first chat it saw — which is how
    # a six-hour digest ended up posted in someone's public channel. The person's
    # own chat is the one whose type is "private".
    chat_id = None
    for result in data.get("result", []):
        msg = result.get("message") or result.get("my_chat_member", {})
        chat = msg.get("chat") if isinstance(msg, dict) else None
        if chat and chat.get("id") and chat.get("type") == "private":
            chat_id = str(chat["id"])
            break

    if not chat_id:
        raise HTTPException(
            404,
            "No private chat found. Send /start to your bot from your own Telegram account, then "
            "try again. A channel the bot administers does not count — digests there would be public.",
        )

    # Persist all Telegram config and enable notifications
    config.save_settings({
        "telegram_bot_token": bot_token,
        "telegram_chat_id": chat_id,
        "telegram_enabled": True,
    })

    return {"status": "success", "message": f"Connected — chat ID {chat_id}"}


@router.post("/settings/telegram/test")
async def test_telegram():
    """Send a test message via the configured Telegram bot.

    Verifies end-to-end connectivity: reads saved token/chat_id from settings,
    sends a formatted HTML message through the Telegram Bot API, and reports
    success or failure back to the frontend.
    """
    settings = config.get_settings()
    token = settings.get("telegram_bot_token")
    chat_id = settings.get("telegram_chat_id")
    if not token or not chat_id:
        raise HTTPException(400, "Telegram is not connected")
    from polling.telegram import is_private_chat
    if not is_private_chat(chat_id):
        raise HTTPException(400, "Your notification chat is a channel or group — digests there would be "
                                 "public, so nothing is sent. Disconnect, then reconnect by sending /start "
                                 "to the bot from your own account.")

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": "✅ <b>PawPoller</b>\nTest notification — Telegram is working!", "parse_mode": "HTML"},
            )
            data = resp.json()
    except Exception as e:
        raise HTTPException(502, f"Failed to send message: {e}")

    if not data.get("ok"):
        desc = data.get("description", "Unknown error")
        raise HTTPException(502, f"Telegram error: {desc}")

    return {"status": "success", "message": "Test message sent"}


@router.post("/settings/telegram/disconnect")
def disconnect_telegram():
    """Clear Telegram token and chat_id from settings, disable notifications."""
    config.delete_settings_keys(["telegram_bot_token", "telegram_chat_id"])
    config.save_settings({"telegram_enabled": False})
    return {"status": "success", "message": "Telegram disconnected"}


# ── Telegram channel POSTING (Posts-module broadcast target) ─────
# Distinct from notifications above: this publishes your composed Posts to a
# channel the bot administers. Since 4.8.0 the posting bot is its own bot —
# the notification bot is never borrowed, so the two jobs cannot be confused.

@router.get("/settings/telegram/channel")
def get_telegram_channel():
    """Channel-posting config for the settings form (never returns the token)."""
    settings = config.get_settings()
    own = settings.get("tg_bot_token", "")
    return {
        "channel": settings.get("tg_channel", ""),
        "has_own_token": bool(own),
        # 4.8.0: the notification bot is never borrowed for channel posting.
        "uses_notification_bot": False,
        "needs_own_token": not own,
        "configured": bool(settings.get("tg_channel") and own),
        # Channel-wide defaults for published work. Each is overridable per
        # artwork via categories.tg in art.json — see posting/platforms/telegram.py.
        "protect": bool(settings.get("tg_protect")),
        "document": bool(settings.get("tg_document")),
        "silent": bool(settings.get("tg_silent")),
        "no_tags": bool(settings.get("tg_no_tags")),
    }


@router.post("/settings/telegram/channel")
def save_telegram_channel(body: dict):
    """Save the target channel + the posting bot's token. A blank token keeps the
    stored one; there is no fallback to the notification bot (4.8.0), and the
    notification bot's own token is refused here. The channel is normalised
    loosely (@name / name / t.me link all accepted)."""
    update = {}
    if "channel" in body:
        update["tg_channel"] = str(body.get("channel") or "").strip()
    # Channel defaults. Stored as real bools so posting/platforms/telegram.py's
    # _flag() never has to guess at a string coming back out of settings.
    for field, key in (("protect", "tg_protect"), ("document", "tg_document"),
                       ("silent", "tg_silent"), ("no_tags", "tg_no_tags")):
        if field in body:
            update[key] = bool(body.get(field))
    if body.get("bot_token"):
        tok = str(body["bot_token"]).strip()
        if tok == (config.get_settings().get("telegram_bot_token") or "").strip():
            raise HTTPException(400, "That is your notification bot's token. Channel posting needs its own "
                                     "bot (4.8.0) — make one in @BotFather and paste that token here.")
        update["tg_bot_token"] = tok
    config.save_settings(update)
    return {"status": "saved", "channel": config.get_settings().get("tg_channel", "")}


@router.post("/settings/telegram/channel/detect")
async def detect_telegram_channels():
    """List the channels this bot can see, so nobody has to type an identifier.

    Channel posting shipped in 2.198.0 asking the user to type the channel by
    hand, and that is where the failures came from. A private channel has **no
    username at all** — the title on screen is not a handle, and neither is a
    ``t.me/+hash`` invite link — so it is reachable only by a numeric -100… id
    that Telegram's UI never shows you.

    Worse than merely hard: a bare name gets prefixed to ``@name`` upstream, and
    a stranger's public channel may already own that username. Observed live —
    a user's private channel titled "Testing" sent us to ``@testing``, an
    unrelated public channel, which passed the getChat check (any public channel
    is readable) and then refused the post. The check confirmed the wrong chat.

    This is the SAME trick the notification-bot setup has always used (see the
    flow comment above: "send /start, we call getUpdates and find the chat_id").
    The bot is already required to be an admin of the channel, and an admin
    receives ``channel_post`` updates — so once anything is posted there, the
    channel identifies itself and the user never types an id.
    """
    settings = config.get_settings()
    token = settings.get("tg_bot_token", "")
    if not token:
        raise HTTPException(400, "No posting bot token — channel posting needs its own bot "
                                 "(Settings → Telegram → Channel posting)")

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(f"https://api.telegram.org/bot{token}/getUpdates")
            data = resp.json()
    except Exception as e:
        raise HTTPException(502, f"Failed to contact Telegram API: {e}")

    if not data.get("ok"):
        desc = data.get("description") or "Telegram rejected the request"
        if data.get("error_code") == 409:
            # The notification bot long-polls getUpdates (polling/telegram_bot.py).
            # Reusing that same token for posting puts the two in contention.
            raise HTTPException(409, "This bot token is already being long-polled for "
                                     "notification commands. Use a SEPARATE bot for channel "
                                     "posting, or stop the notification bot briefly.")
        raise HTTPException(400, f"Telegram rejected the token: {desc}")

    found: dict[str, dict] = {}
    for result in data.get("result", []):
        for key in ("channel_post", "edited_channel_post", "my_chat_member", "message"):
            obj = result.get(key)
            chat = obj.get("chat") if isinstance(obj, dict) else None
            if not chat or not chat.get("id"):
                continue
            if chat.get("type") not in ("channel", "supergroup", "group"):
                continue
            found[str(chat["id"])] = {
                "id": str(chat["id"]),
                "title": chat.get("title") or "",
                "username": chat.get("username") or "",
                "type": chat.get("type") or "",
            }

    if not found:
        raise HTTPException(404,
            "No channels seen yet. Add the bot to your channel as an admin with "
            "'Post Messages', then post any message in the channel and press this "
            "again — an admin bot receives channel posts, which is how it learns the id.")

    return {"channels": list(found.values())}


@router.post("/settings/telegram/channel/test")
async def test_telegram_channel(body: dict | None = None):
    """Validate the channel: getChat via the resolved bot token, then send a test
    message so the user sees it actually lands. Uses the just-typed channel if
    provided, else the saved one."""
    settings = config.get_settings()
    channel = ((body or {}).get("channel") or settings.get("tg_channel") or "").strip()
    token = settings.get("tg_bot_token", "")
    if not token:
        raise HTTPException(400, "No posting bot token — channel posting needs its own bot "
                                 "(Settings → Telegram → Channel posting)")
    if not channel:
        raise HTTPException(400, "No channel set")
    from clients.tg.client import TgClient
    client = TgClient(bot_token=token, channel=channel)
    err = await client.validate()
    if err:
        raise HTTPException(400, f"Channel check failed: {err}")

    # Name the channel we actually reached. getChat succeeds against ANY public
    # channel, so "the check passed" is not evidence we found the user's one:
    # a bare name is prefixed to "@name" upstream, and a stranger's public
    # channel may already own that username. Observed live — a private channel
    # titled "Testing" sent us to @testing, someone else's channel, which
    # validated cleanly and then refused the post. Reporting the resolved title
    # and username is what turns that from a mystery into something the user can
    # see at a glance.
    chat = getattr(client, "resolved_chat", {}) or {}
    who = chat.get("title") or chat.get("username") or channel
    handle = f" (@{chat['username']})" if chat.get("username") else ""
    reached = f"{who}{handle}"
    try:
        r = await client.create_post("✅ PawPoller is connected to this channel.")
    except Exception as e:
        raise HTTPException(400, f"Test post failed: {e}")
    if not r:
        # Report what Telegram said, not what we suspect. getChat has already
        # succeeded by this point, so the channel resolves and the token is
        # valid — guessing "is the bot an admin?" at a user who has just made it
        # an admin sends them to re-check the one thing that is provably fine.
        # The API names the cause ("not enough rights to send text messages to
        # the chat", "have no rights to send a message", "CHAT_WRITE_FORBIDDEN").
        reason = getattr(client, "last_error", "") or "Telegram gave no reason"
        hint = ""
        if "right" in reason.lower() or "forbidden" in reason.lower():
            # Two very different causes look identical here, and the second is
            # the one people never suspect, so both are named.
            hint = (f" — the handle '{client.channel}' resolved to “{reached}”. "
                    "If that is NOT your channel, that is the problem: a bare name is "
                    "treated as a public @username, and someone else may own it. A "
                    "private channel has no username at all — use its numeric -100… id. "
                    "If it IS your channel, open the bot in the channel's Administrators "
                    "list and turn on 'Post Messages'.")
        raise HTTPException(400, f"Telegram accepted the channel but refused the post: {reason}{hint}")
    return {"status": "success",
            "message": f"Test message posted to “{reached}” — check it is the right channel",
            "url": r.get("url", "")}


@router.get("/settings/telegram/features")
def get_telegram_features():
    """Return Telegram notification feature toggles."""
    settings = config.get_settings()
    return {
        "poll_summaries": settings.get("telegram_poll_summaries", True),
        "error_alerts": settings.get("telegram_error_alerts", True),
        "milestones": settings.get("telegram_milestones", True),
        "digest": settings.get("telegram_digest", True),
        "digest_interval_hours": settings.get("telegram_digest_interval_hours", 6),
    }


@router.post("/settings/telegram/features")
def set_telegram_features(body: dict):
    """Update Telegram notification feature toggles."""
    update = {}
    for key in ("telegram_poll_summaries", "telegram_error_alerts",
                "telegram_milestones", "telegram_digest"):
        short = key.replace("telegram_", "")
        if short in body:
            update[key] = bool(body[short])
    if "digest_interval_hours" in body:
        val = int(body["digest_interval_hours"])
        update["telegram_digest_interval_hours"] = max(1, min(val, 168))
    if update:
        config.save_settings(update)
    return {"status": "success"}


@router.post("/settings/telegram/digest")
async def send_digest_now():
    """Manually trigger a 6-hourly digest report."""
    from polling.telegram import send_digest_report
    try:
        await send_digest_report()
        return {"status": "success", "message": "Digest sent"}
    except Exception as e:
        raise HTTPException(500, f"Failed to send digest: {e}")


# ── CSV Export ────────────────────────────────────────────────
# CSV export uses the DictWriter -> StreamingResponse pattern:
#   1. Query returns a list of dicts (rows from the database)
#   2. DictWriter writes header + rows into a StringIO buffer
#   3. The buffer content is wrapped in a StreamingResponse with
#      Content-Disposition header for browser download
#   4. If no rows exist, a simple "No data" text response is returned
#
# This avoids loading the entire CSV into memory as a string before
# sending, though for practical dataset sizes it would not matter.

def _sanitize_csv_value(val):
    """Prevent CSV formula injection (OWASP recommendation).

    Excel/LibreOffice treat cells starting with =, +, -, @, \\t, \\r as
    formulas.  A malicious submission title like '=CMD("calc")' would
    execute when the exported CSV is opened.  Prefixing with a single
    quote neutralises the formula while remaining human-readable.
    """
    if isinstance(val, str) and val and val[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + val
    return val


def _csv_response(rows: list[dict], filename: str) -> StreamingResponse:
    """Generate a CSV StreamingResponse from a list of dicts.

    Uses csv.DictWriter to auto-generate the header row from dict keys,
    then writes all rows.  String values are sanitised against CSV formula
    injection before writing.  The result is wrapped in a StreamingResponse
    with a Content-Disposition attachment header so browsers trigger a download.
    """
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


@router.get("/export/submissions")
def export_ib_submissions():
    """Export all Inkbunny submissions as a CSV file download."""
    conn = get_connection()
    try:
        subs = queries.get_all_submissions(conn)
        return _csv_response(subs, "inkbunny_submissions.csv")
    finally:
        conn.close()


@router.get("/export/snapshots")
def export_ib_snapshots(id: Optional[int] = Query(None)):
    """Export snapshots as CSV. If an ID is provided, export only that submission's
    snapshots; otherwise export all snapshots across all submissions."""
    conn = get_connection()
    try:
        if id:
            snaps = queries.get_snapshots(conn, id)
        else:
            snaps = [dict(r) for r in conn.execute("SELECT * FROM snapshots ORDER BY polled_at ASC").fetchall()]
        return _csv_response(snaps, f"inkbunny_snapshots{'_' + str(id) if id else ''}.csv")
    finally:
        conn.close()


# ── Groups ───────────────────────────────────────────────────
# Groups allow users to organise submissions into named collections
# for aggregate tracking. Each group can contain members from any
# platform (IB, FA, WS). Standard CRUD operations are provided:
#   - GET    /groups              : list all groups
#   - POST   /groups              : create a new group
#   - PUT    /groups/{id}         : update group name/description
#   - DELETE /groups/{id}         : delete a group and its memberships
#   - POST   /groups/{id}/members : add a submission to a group
#   - DELETE /groups/{id}/members : remove a submission from a group
#   - GET    /groups/{id}/stats   : aggregate stats for all group members

@router.get("/groups")
def list_groups():
    """List all groups with their metadata."""
    conn = get_connection()
    try:
        return {"groups": group_queries.get_all_groups(conn)}
    finally:
        conn.close()


@router.post("/groups")
def create_group(body: dict):
    """Create a new group with a name and optional description."""
    name = body.get("name", "").strip()
    if not name:
        raise HTTPException(400, "Group name is required")
    conn = get_connection()
    try:
        group_id = group_queries.create_group(conn, name, body.get("description", ""))
        return {"status": "success", "group_id": group_id}
    finally:
        conn.close()


@router.put("/groups/{group_id}")
def update_group(group_id: int, body: dict):
    """Update a group's name and/or description."""
    conn = get_connection()
    try:
        group_queries.update_group(conn, group_id, body.get("name"), body.get("description"))
        return {"status": "success"}
    finally:
        conn.close()


@router.delete("/groups/{group_id}")
def delete_group(group_id: int):
    """Delete a group and all its membership records."""
    conn = get_connection()
    try:
        group_queries.delete_group(conn, group_id)
        return {"status": "success"}
    finally:
        conn.close()


@router.post("/groups/{group_id}/members")
def add_group_member(group_id: int, body: dict):
    """Add a submission to a group. Requires platform (ib/fa/ws) and submission_id.

    Members are identified by the combination of platform + submission_id,
    since submission IDs are only unique within each platform.
    """
    platform = body.get("platform", "")
    submission_id = body.get("submission_id")
    if not platform or not submission_id:
        raise HTTPException(400, "platform and submission_id are required")
    conn = get_connection()
    try:
        added = group_queries.add_group_member(conn, group_id, platform, int(submission_id))
        return {"status": "success", "added": added}
    finally:
        conn.close()


@router.delete("/groups/{group_id}/members")
def remove_group_member(group_id: int, platform: str = Query(...), submission_id: int = Query(...)):
    """Remove a submission from a group by platform + submission_id."""
    conn = get_connection()
    try:
        group_queries.remove_group_member(conn, group_id, platform, submission_id)
        return {"status": "success"}
    finally:
        conn.close()


@router.get("/groups/{group_id}/stats")
def get_group_stats(group_id: int):
    """Get aggregate statistics for all submissions in a group.

    Returns combined views/faves/comments totals and per-member breakdowns
    across all platforms represented in the group.
    """
    conn = get_connection()
    try:
        return group_queries.get_group_stats(conn, group_id)
    finally:
        conn.close()


# ── Analytics ────────────────────────────────────────────────
# Analytics endpoints provide cross-submission insights:
#   - top-fans: users who have faved the most submissions (loyal followers)
#   - trending: submissions with above-average growth in a recent time window,
#     identified by a multiplier threshold against the baseline growth rate

@router.get("/analytics/top-fans")
def get_top_fans(limit: int = Query(20, ge=1, le=100)):
    """Get the top fans -- users who have faved the most submissions.

    Aggregates faving_users across all submissions to find the most engaged
    followers. Limited to a configurable count (default 20, max 100).
    """
    conn = get_connection()
    try:
        return {"fans": analytics_queries.get_top_fans(conn, limit)}
    finally:
        conn.close()


@router.get("/analytics/trending")
def get_trending(hours: int = Query(24, ge=1), threshold: float = Query(2.0, ge=0.5)):
    """Get trending submissions -- those with above-average growth recently.

    Parameters:
      - hours: lookback window (e.g., 24 = last 24 hours)
      - threshold: multiplier above average growth to qualify as "trending"
        (e.g., 2.0 means a submission must be growing at 2x the average rate)
    """
    conn = get_connection()
    try:
        return {"trending": analytics_queries.get_trending_submissions(conn, hours, threshold)}
    finally:
        conn.close()


# ── Cross-Platform Links ────────────────────────────────────
# Links connect the same artwork/story posted across multiple platforms
# (e.g., the same piece on IB, FA, and WS). This enables combined stats
# views and comparison charts across platforms for the same content.
#   - GET    /links              : list all links with their members
#   - POST   /links              : create a link (requires >= 2 members)
#   - DELETE /links/{id}         : delete a link
#   - GET    /links/{id}/stats   : combined stats across all linked submissions
#   - GET    /links/{id}/snapshots : combined time-series for charting
#   - GET    /links/suggestions  : auto-detected links based on title similarity

@router.get("/links")
def list_links():
    """List all cross-platform links with their member submissions."""
    conn = get_connection()
    try:
        return {"links": analytics_queries.get_links(conn)}
    finally:
        conn.close()


@router.post("/links")
def create_link(body: dict):
    """Create a cross-platform link between 2+ submissions.

    Each member is a {platform, submission_id} pair. At least 2 members
    are required (linking a single submission to itself is meaningless).
    """
    members = body.get("members", [])
    if len(members) < 2:
        raise HTTPException(400, "At least 2 members required")
    conn = get_connection()
    try:
        link_id = analytics_queries.create_link(conn, members)
        return {"status": "success", "link_id": link_id}
    finally:
        conn.close()


@router.delete("/links/{link_id}")
def delete_link(link_id: int):
    """Delete a cross-platform link and its membership records."""
    conn = get_connection()
    try:
        analytics_queries.delete_link(conn, link_id)
        return {"status": "success"}
    finally:
        conn.close()


@router.get("/links/{link_id}/stats")
def get_link_stats(link_id: int):
    """Get combined statistics across all submissions in a link.

    Aggregates views/faves/comments from all linked submissions to show
    the total reach of a piece of content across platforms.
    """
    conn = get_connection()
    try:
        return analytics_queries.get_link_combined_stats(conn, link_id)
    finally:
        conn.close()


@router.get("/links/{link_id}/snapshots")
def get_link_snapshots(link_id: int):
    """Get combined time-series snapshots for all submissions in a link.

    Merges snapshot data from all linked submissions into a unified
    time-series for cross-platform growth charting.
    """
    conn = get_connection()
    try:
        return {"snapshots": analytics_queries.get_link_combined_snapshots(conn, link_id)}
    finally:
        conn.close()


@router.get("/links/suggestions")
def get_link_suggestions():
    """Auto-suggest potential cross-platform links based on title similarity.

    Scans submissions across IB, FA, and WS for matching or similar titles
    that likely represent the same content posted on multiple platforms.
    """
    conn = get_connection()
    try:
        return {"suggestions": analytics_queries.auto_suggest_links(conn)}
    finally:
        conn.close()


# ── Auto-Update ──────────────────────────────────────────────
# The auto-update system has two steps:
#   1. GET  /update/check : checks GitHub releases (or similar) for a newer
#      version. Returns version info and download_url if an update is available.
#   2. POST /update/apply : downloads the update zip from the provided URL,
#      extracts it over the current installation, and triggers a restart.
# This two-step approach lets the frontend show the user what version is
# available before they commit to applying the update.

@router.get("/update/check")
def check_update():
    """Check for available updates. Returns version info and download URL if newer."""
    return updater.check_for_update()


@router.post("/update/apply")
def apply_update(body: dict):
    """Download and apply an update from the given URL.

    Flow:
      1. Download the update zip from the provided download_url
      2. Extract and overwrite the current installation files
      3. Return success -- the server will restart to load the new version
    """
    download_url = body.get("download_url", "")
    if not download_url:
        raise HTTPException(400, "download_url is required")
    # Security: only allow downloads from the official GitHub repository
    parsed = urlparse(download_url)
    if not parsed.hostname or not (
        parsed.hostname == "github.com"
        or parsed.hostname.endswith(".github.com")
        or parsed.hostname == "api.github.com"
        or parsed.hostname.endswith(".githubusercontent.com")
    ):
        raise HTTPException(400, "Only GitHub URLs are allowed for updates")
    try:
        zip_path = updater.download_update(download_url)
        updater.apply_update(zip_path)
    except Exception as e:
        raise HTTPException(500, detail=str(e))
    # apply_update spawned a detached helper (Windows _update.bat / Linux shell
    # script) that waits a few seconds, then replaces this app's files — which
    # requires THIS process to exit first so it releases the lock on its own
    # .exe/DLLs (Windows) or AppImage (Linux). Without this the helper's robocopy
    # hits the running, locked exe and only partial-copies (the 3.0.0 incident).
    # Mirror the /settings restart endpoint: hard-exit shortly after we respond.
    import os as _os
    threading.Timer(1.5, lambda: _os._exit(0)).start()
    return {"status": "success", "message": "Update applied — restarting..."}


# ── Thumbnail Proxy ──────────────────────────────────────────
# Inkbunny's CDN (metapix.net) does not set CORS headers, so the browser
# blocks direct image loads from the frontend. This endpoint proxies
# thumbnail requests through the local server to bypass CORS restrictions.
#
# Security: a domain whitelist restricts proxying to metapix.net only,
# preventing this endpoint from being used as an open proxy to arbitrary URLs.
# Responses are cached for 24 hours (86400 seconds) to reduce repeat fetches.

@router.get("/thumb")
async def proxy_thumbnail(url: str = Query(..., description="Inkbunny thumbnail URL")):
    """Proxy Inkbunny thumbnails to avoid cross-origin blocking.

    Only allows URLs from the metapix.net domain (Inkbunny's CDN).
    This whitelist prevents abuse of this proxy endpoint for arbitrary URLs.
    Responses include a Cache-Control header for 24-hour browser caching.
    """
    parsed = urlparse(url)
    # Domain whitelist: only proxy requests to Inkbunny's CDN (metapix.net)
    if not parsed.hostname or not (
        parsed.hostname == "metapix.net" or parsed.hostname.endswith(".metapix.net")
    ):
        raise HTTPException(400, "Only Inkbunny CDN URLs allowed")
    try:
        resp = await _thumb_client.get(url)
        resp.raise_for_status()
        content_type = resp.headers.get("content-type", "image/jpeg")
        return Response(content=resp.content, media_type=content_type,
                        headers={"Cache-Control": "public, max-age=86400"})
    except Exception as e:
        logger.warning("Thumb proxy failed for %s: %s", url, e)
        raise HTTPException(502, detail="Failed to fetch thumbnail")


# ── Pinned Submissions ────────────────────────────────────────

@router.get("/pins")
def get_pins():
    """Return the list of pinned submissions with current stats."""
    settings = config.get_settings()
    pins = settings.get("pinned_submissions", [])
    result = []
    conn = get_connection()
    try:
        table_map = {"ib": "submissions", "fa": "fa_submissions", "ws": "ws_submissions", "sf": "sf_submissions", "sqw": "sqw_submissions", "ao3": "ao3_submissions", "da": "da_submissions", "wp": "wp_submissions", "ik": "ik_submissions", "bsky": "bsky_submissions", "tw": "tw_submissions", "mast": "mast_submissions", "tum": "tum_submissions", "pix": "pix_submissions", "thr": "thr_submissions", "ig": "ig_submissions", "e621": "e621_submissions"}
        for pin in pins:
            table = table_map.get(pin.get("platform"))
            if not table:
                continue
            try:
                row = conn.execute(
                    f"SELECT * FROM {table} WHERE submission_id = ?",
                    (pin["submission_id"],),
                ).fetchone()
            except Exception:
                continue
            if row:
                d = dict(row)
                d["platform"] = pin["platform"]
                result.append(d)
    finally:
        conn.close()
    return {"pins": result}


@router.post("/pins")
def add_pin(body: dict):
    """Pin a submission. Body: { platform, submission_id }. Max 10 pins."""
    platform = body.get("platform", "")
    sub_id = body.get("submission_id")
    if not platform or sub_id is None:
        raise HTTPException(400, "platform and submission_id required")
    settings = config.get_settings()
    pins = settings.get("pinned_submissions", [])
    if any(p["platform"] == platform and str(p["submission_id"]) == str(sub_id) for p in pins):
        return {"status": "already_pinned"}
    if len(pins) >= 10:
        raise HTTPException(400, "Maximum 10 pins allowed")
    pins.append({"platform": platform, "submission_id": sub_id})
    config.save_settings({"pinned_submissions": pins})
    return {"status": "pinned"}


@router.delete("/pins")
def remove_pin(platform: str = Query(...), submission_id: str = Query(...)):
    """Unpin a submission."""
    settings = config.get_settings()
    pins = settings.get("pinned_submissions", [])
    pins = [p for p in pins if not (p["platform"] == platform and str(p["submission_id"]) == str(submission_id))]
    config.save_settings({"pinned_submissions": pins})
    return {"status": "unpinned"}


# ── Goal Tracking ─────────────────────────────────────────────

@router.get("/goals")
def get_goals():
    """Return all goals with computed current values and progress."""
    conn = get_connection()
    try:
        rows = conn.execute("SELECT * FROM goals ORDER BY created_at DESC").fetchall()
        result = []
        table_map = {"ib": "submissions", "fa": "fa_submissions", "ws": "ws_submissions", "sf": "sf_submissions", "sqw": "sqw_submissions", "ao3": "ao3_submissions", "da": "da_submissions", "wp": "wp_submissions", "ik": "ik_submissions", "bsky": "bsky_submissions", "tw": "tw_submissions", "mast": "mast_submissions", "tum": "tum_submissions", "pix": "pix_submissions", "thr": "thr_submissions", "ig": "ig_submissions", "e621": "e621_submissions"}
        for row in rows:
            g = dict(row)
            metric = g["metric"]
            current = 0
            title = None
            # Validate metric against the shared whitelist before SQL interpolation
            if metric not in config.ALLOWED_GOAL_METRICS:
                g["current_value"] = 0
                g["submission_title"] = None
                g["progress_pct"] = 0
                result.append(g)
                continue
            if g["scope"] == "submission" and g["submission_id"]:
                table = table_map.get(g["platform"])
                if table:
                    try:
                        sub = conn.execute(
                            f"SELECT title, {metric} FROM {table} WHERE submission_id = ?",
                            (g["submission_id"],),
                        ).fetchone()
                        if sub:
                            title = sub["title"]
                            current = sub[metric] or 0
                    except Exception:
                        pass
            else:
                if g["platform"] == "all":
                    for tbl in table_map.values():
                        try:
                            r = conn.execute(f"SELECT COALESCE(SUM({metric}), 0) as total FROM {tbl}").fetchone()
                            current += r["total"]
                        except Exception:
                            pass
                else:
                    table = table_map.get(g["platform"])
                    if table:
                        try:
                            r = conn.execute(f"SELECT COALESCE(SUM({metric}), 0) as total FROM {table}").fetchone()
                            current = r["total"]
                        except Exception:
                            pass
            g["current_value"] = current
            g["submission_title"] = title
            g["progress_pct"] = min(100, round((current / g["target_value"]) * 100)) if g["target_value"] > 0 else 0
            result.append(g)
        return {"goals": result}
    finally:
        conn.close()


@router.post("/goals")
def create_goal(body: dict):
    """Create a new goal. Body: { platform, scope, submission_id?, metric, target_value }."""
    platform = body.get("platform", "ib")
    scope = body.get("scope", "account")
    sub_id = body.get("submission_id")
    metric = body.get("metric", "views")
    target = int(body.get("target_value", 0))
    if metric not in config.ALLOWED_GOAL_METRICS:
        raise HTTPException(400, "Invalid metric")
    if target <= 0:
        raise HTTPException(400, "Target must be positive")
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO goals (platform, scope, submission_id, metric, target_value) VALUES (?, ?, ?, ?, ?)",
            (platform, scope, sub_id, metric, target),
        )
        conn.commit()
        return {"status": "created"}
    finally:
        conn.close()


@router.delete("/goals/{goal_id}")
def delete_goal(goal_id: int):
    """Delete a goal."""
    conn = get_connection()
    try:
        conn.execute("DELETE FROM goals WHERE goal_id = ?", (goal_id,))
        conn.commit()
        return {"status": "deleted"}
    finally:
        conn.close()


def _get_submission_tags(conn, platform: str, submission_id) -> list:
    """Get tags assigned to a specific submission."""
    try:
        rows = conn.execute(
            "SELECT t.tag_id, t.name, t.color FROM tags t "
            "JOIN submission_tags st ON t.tag_id = st.tag_id "
            "WHERE st.platform = ? AND st.submission_id = ?",
            (platform, submission_id),
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []

# ── Tags / Submission Categorisation ──────────────────────────

@router.get("/tags")
def get_tags():
    """Return all tags with submission counts."""
    conn = get_connection()
    try:
        tags = conn.execute("SELECT * FROM tags ORDER BY name").fetchall()
        result = []
        for t in tags:
            d = dict(t)
            count = conn.execute("SELECT COUNT(*) as c FROM submission_tags WHERE tag_id = ?", (t["tag_id"],)).fetchone()
            d["submission_count"] = count["c"]
            result.append(d)
        return {"tags": result}
    finally:
        conn.close()


@router.post("/tags")
def create_tag(body: dict):
    """Create a tag. Body: { name, color? }."""
    name = body.get("name", "").strip()
    if not name:
        raise HTTPException(400, "Tag name required")
    color = body.get("color", "#6c8cff")
    conn = get_connection()
    try:
        cursor = conn.execute("INSERT INTO tags (name, color) VALUES (?, ?)", (name, color))
        conn.commit()
        return {"status": "created", "tag_id": cursor.lastrowid}
    except sqlite3.IntegrityError:
        raise HTTPException(409, "Tag already exists")
    finally:
        conn.close()


@router.delete("/tags/{tag_id}")
def delete_tag(tag_id: int):
    """Delete a tag and all its associations."""
    conn = get_connection()
    try:
        conn.execute("DELETE FROM tags WHERE tag_id = ?", (tag_id,))
        conn.commit()
        return {"status": "deleted"}
    finally:
        conn.close()


@router.post("/tags/{tag_id}/submissions")
def add_tag_to_submission(tag_id: int, body: dict):
    """Assign a tag to a submission. Body: { platform, submission_id }."""
    platform = body.get("platform")
    sub_id = body.get("submission_id")
    if not platform or sub_id is None:
        raise HTTPException(400, "platform and submission_id required")
    conn = get_connection()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO submission_tags (tag_id, platform, submission_id) VALUES (?, ?, ?)",
            (tag_id, platform, sub_id),
        )
        conn.commit()
        return {"status": "tagged"}
    finally:
        conn.close()


@router.delete("/tags/{tag_id}/submissions")
def remove_tag_from_submission(tag_id: int, platform: str = Query(...), submission_id: str = Query(...)):
    """Remove a tag from a submission."""
    conn = get_connection()
    try:
        conn.execute(
            "DELETE FROM submission_tags WHERE tag_id = ? AND platform = ? AND submission_id = ?",
            (tag_id, platform, submission_id),
        )
        conn.commit()
        return {"status": "untagged"}
    finally:
        conn.close()


@router.get("/tags/{tag_id}/stats")
def get_tag_stats(tag_id: int):
    """Aggregate stats for all submissions with a given tag."""
    conn = get_connection()
    try:
        members = conn.execute("SELECT platform, submission_id FROM submission_tags WHERE tag_id = ?", (tag_id,)).fetchall()
        # Table + metric columns from the canonical registry
        # (database/platform_metrics.py) — the local copy this replaces was
        # missing FurryNetwork/Furbooru and summed e621's net score as views.
        total_views = total_score = total_faves = total_comments = 0
        subs = []
        for m in members:
            plat = m["platform"]
            spec = platform_metrics.get(plat)
            if not spec:
                continue
            try:
                row = conn.execute(
                    f"SELECT * FROM {spec.table} WHERE submission_id = ?",
                    (m["submission_id"],),
                ).fetchone()
            except sqlite3.Error as e:
                logger.warning("tag stats: %s unavailable (%s): %s", plat, spec.table, e)
                continue
            if row:
                d = dict(row)
                d["platform"] = plat
                total_views += (d.get(spec.views, 0) or 0) if spec.views else 0
                total_score += (d.get(spec.score, 0) or 0) if spec.score else 0
                total_faves += (d.get(spec.faves, 0) or 0) if spec.faves else 0
                total_comments += (d.get(spec.comments, 0) or 0) if spec.comments else 0
                subs.append(d)
        return {"total_views": total_views, "total_score": total_score,
                "total_favorites": total_faves, "total_comments": total_comments,
                "submissions": subs}
    finally:
        conn.close()


# ── Backup & Restore ──────────────────────────────────────────

@router.get("/backup/database")
def download_backup():
    """Download a consistent backup of the SQLite database."""
    conn = get_connection()
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    db_bytes = config.DB_PATH.read_bytes()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return Response(
        content=db_bytes,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="pawpoller_backup_{ts}.db"'},
    )


@router.post("/backup/restore")
async def restore_backup(file: UploadFile = File(...)):
    """Restore the database from an uploaded .db file."""
    content = await file.read()
    if len(content) < 100:
        raise HTTPException(400, "File too small to be a valid database")
    # Validate it's a SQLite file (magic bytes)
    if content[:16] != b"SQLite format 3\x00":
        raise HTTPException(400, "Not a valid SQLite database file")
    # Write to a temp file and validate expected tables exist
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        tmp.write(content)
        tmp_path = tmp.name
    try:
        test_conn = sqlite3.connect(tmp_path)
        tables = {r[0] for r in test_conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        test_conn.close()
        if "submissions" not in tables:
            raise HTTPException(400, "Database does not contain expected PawPoller tables")
        # Checkpoint current DB and replace
        conn = get_connection()
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()
        shutil.copy2(tmp_path, str(config.DB_PATH))
        # Remove stale WAL/SHM files from the old database to prevent
        # SQLite from replaying them against the restored database.
        wal_path = Path(str(config.DB_PATH) + "-wal")
        shm_path = Path(str(config.DB_PATH) + "-shm")
        if wal_path.exists():
            wal_path.unlink()
        if shm_path.exists():
            shm_path.unlink()
        init_db()
        return {"status": "restored", "tables_found": len(tables)}
    finally:
        try:
            import os
            os.unlink(tmp_path)
        except OSError:
            pass


# ── Application Logs ─────────────────────────────────────────

@router.get("/logs")
def get_logs(lines: int = Query(200, ge=10, le=2000), file: str = Query("server")):
    """Return the last N lines of a log file.

    Reads from LOGS_DIR/{file}.log.  Only whitelisted filenames are allowed
    to prevent path traversal.  Returns newest lines last (natural log order).
    """
    allowed = {"server", "app", "polling"}
    if file not in allowed:
        raise HTTPException(400, f"Invalid log file. Allowed: {', '.join(sorted(allowed))}")
    log_path = config.LOGS_DIR / f"{file}.log"
    if not log_path.exists():
        return {"lines": [], "file": file, "total_lines": 0}
    try:
        # Read the file and return the tail
        all_lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        tail = all_lines[-lines:] if len(all_lines) > lines else all_lines
        return {"lines": tail, "file": file, "total_lines": len(all_lines)}
    except OSError as e:
        raise HTTPException(500, f"Failed to read log file: {e}")


_LOG_TAIL_ALLOWED = {"server", "app", "polling"}


def _log_sse(payload: dict) -> bytes:
    return f"data: {json.dumps(payload, default=str)}\n\n".encode("utf-8")


@router.get("/logs/stream")
async def stream_logs(
    file: str = Query("app"),
    backfill: int = Query(50, ge=0, le=500),
):
    """SSE tail-follow for a log file.

    Initial backfill of the last N lines, then polls the file every
    500ms and emits any newly-appended lines as SSE events. Heartbeat
    every 15s so reverse-proxy intermediates don't reap the stream.
    Recovers from log rotation by detecting size shrinkage and
    re-seeking from the new file's start.

    Backs the floating logs panel — opt-in widget on the dashboard
    that surfaces app.log / polling.log without an SSH session. The
    one-shot /api/logs endpoint above is still preferred for static
    snapshots; this one is for live tailing.
    """
    if file not in _LOG_TAIL_ALLOWED:
        raise HTTPException(
            400,
            f"Invalid log file. Allowed: {', '.join(sorted(_LOG_TAIL_ALLOWED))}",
        )
    log_path = config.LOGS_DIR / f"{file}.log"

    async def gen():
        # Initial backfill — read the last N lines so the panel
        # opens with context, not blank.
        if log_path.exists():
            try:
                all_lines = log_path.read_text(
                    encoding="utf-8", errors="replace",
                ).splitlines()
                tail = all_lines[-backfill:] if backfill > 0 else []
                for line in tail:
                    yield _log_sse({"line": line, "backfill": True})
            except Exception as e:
                yield _log_sse({"event": "error", "message": str(e)})

        # Tail-follow loop. We track byte offset rather than line
        # count so partial last-line writes don't get duplicated on
        # the next poll. File-size shrink ⇒ rotation ⇒ reset offset.
        try:
            last_size = log_path.stat().st_size if log_path.exists() else 0
        except OSError:
            last_size = 0
        last_heartbeat = time.monotonic()

        while True:
            try:
                if not log_path.exists():
                    # File hasn't been created yet (first run); wait
                    # politely and emit a heartbeat so the client
                    # knows we're still here.
                    if time.monotonic() - last_heartbeat > 15:
                        yield b": heartbeat\n\n"
                        last_heartbeat = time.monotonic()
                    await asyncio.sleep(1.0)
                    continue
                stat = log_path.stat()
                if stat.st_size < last_size:
                    # Rotation / truncation: reset offset, no need
                    # to backfill (the new file is the head we want).
                    last_size = 0
                if stat.st_size > last_size:
                    with open(log_path, "rb") as f:
                        f.seek(last_size)
                        chunk = f.read(stat.st_size - last_size)
                    last_size = stat.st_size
                    text = chunk.decode("utf-8", errors="replace")
                    # Trailing newline produces an empty trailing
                    # element from splitlines() — fine, we just skip
                    # empties below.
                    for line in text.splitlines():
                        if line:
                            yield _log_sse({"line": line})
                    last_heartbeat = time.monotonic()
                elif time.monotonic() - last_heartbeat > 15:
                    yield b": heartbeat\n\n"
                    last_heartbeat = time.monotonic()
                await asyncio.sleep(0.5)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug("logs/stream tail loop error: %s", e)
                yield _log_sse({"event": "error", "message": str(e)})
                await asyncio.sleep(2.0)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable nginx buffering
        },
    )


# ── Historical Analytics ──────────────────────────────────────

@router.get("/analytics/insights")
def get_analytics_insights(tz_offset: int = 0):
    """Benchmarks + best-time-to-post (gap-wave-3 §2+3). tz_offset = the
    client's minutes-east-of-UTC (JS: -new Date().getTimezoneOffset()) so the
    weekday/hour buckets land in the user's local clock."""
    from database import analytics_queries
    conn = get_connection()
    try:
        return analytics_queries.get_posting_insights(conn, tz_offset_minutes=tz_offset)
    finally:
        conn.close()


@router.get("/analytics/repost-radar")
def get_repost_radar(min_age_days: int = 60, limit: int = 25):
    """Older, well-performing artwork worth resurfacing to your feed (gap-wave-6).

    Fully deterministic — no model, no AI: it ranks YOUR OWN past artwork by the
    pooled view/fave/comment counts the pollers already collected, gated to pieces
    you haven't posted anywhere in ``min_age_days``. Each candidate carries a
    thumbnail, a "last shared N days ago", and the platform links to revisit the
    original post. A follower-context block rides alongside so you can see how much
    your following has grown since — where the (still-young) follower history
    covers the window; it fills in over time.
    """
    from database import analytics_queries, followers
    from posting import artwork_reader
    from urllib.parse import quote

    min_age_days = min(3650, max(7, int(min_age_days)))
    limit = min(100, max(1, int(limit)))
    conn = get_connection()
    try:
        cands = analytics_queries.get_repost_candidates(
            conn, min_age_days=min_age_days, limit=limit)

        # Title + thumbnail from the artwork library — one pass, no per-piece IO.
        art = {a["name"]: a for a in artwork_reader.list_artworks()}
        for c in cands:
            a = art.get(c["name"], {})
            c["title"] = a.get("title") or c["name"].replace("_", " ")
            img = a.get("image", "")
            c["thumb_url"] = (f"/api/artwork/image?name={quote(c['name'])}"
                              f"&file={quote(img)}") if img else ""
            c["detail_route"] = f"#/artwork/image/{quote(c['name'])}"

        # Honest follower-context block: current following + growth over whatever
        # snapshot history exists (tracking is young — this fills in over time).
        foll = []
        try:
            plat_rows = conn.execute(
                "SELECT DISTINCT platform FROM accounts "
                "WHERE follower_count IS NOT NULL AND follower_count > 0"
            ).fetchall()
        except Exception:
            plat_rows = []
        for r in plat_rows:
            plat = r["platform"]
            latest = followers.platform_latest(conn, plat) or {}
            series = followers.platform_series(conn, plat)
            delta = days = None
            if len(series) >= 2:
                first, last = series[0], series[-1]
                delta = (last.get("followers") or 0) - (first.get("followers") or 0)
                d0, _ = analytics_queries._parse_posted(first.get("polled_at"))
                d1, _ = analytics_queries._parse_posted(last.get("polled_at"))
                if d0 and d1:
                    days = (d1 - d0).days
            foll.append({"platform": plat, "followers": latest.get("followers"),
                         "delta": delta, "days": days})
        foll.sort(key=lambda x: -(x["followers"] or 0))

        return {"candidates": cands, "followers": foll,
                "min_age_days": min_age_days}
    finally:
        conn.close()


@router.get("/analytics/tag-performance")
def get_tag_performance_route(min_works: int = 3, limit: int = 40,
                              platform: str = ""):
    """Which tags/keywords earn engagement across your own posts (gap-wave-6).
    Deterministic: each piece is normalised against its platform's median so the
    ranking is fair across platforms. No model. ?platform=fa scopes to one."""
    from database import analytics_queries
    min_works = min(50, max(1, int(min_works)))
    limit = min(200, max(1, int(limit)))
    conn = get_connection()
    try:
        return analytics_queries.get_tag_performance(
            conn, min_works=min_works, limit=limit,
            platform=(platform or None))
    finally:
        conn.close()


# ── Weekly email digest ───────────────────────────────────────

@router.get("/digest/status")
def digest_status():
    """Current weekly-email-digest config (for the Settings panel). Never returns
    the SMTP password — only whether one is stored."""
    from polling import email_digest
    settings = config.get_settings()
    return {
        "enabled": bool(settings.get("email_digest_enabled", False)),
        "interval_days": int(settings.get("email_digest_interval_days", 7) or 7),
        "recipients": email_digest.parse_recipients(settings),
        "recipients_raw": settings.get("email_digest_recipients") or "",
        "smtp_host": settings.get("smtp_host") or email_digest.DEFAULT_SMTP_HOST,
        "smtp_port": int(settings.get("smtp_port") or email_digest.DEFAULT_SMTP_PORT),
        "smtp_username": settings.get("smtp_username") or "",
        "smtp_from": settings.get("smtp_from") or "",
        "smtp_use_tls": bool(settings.get("smtp_use_tls", True)),
        "has_password": bool(settings.get("smtp_password")),
        "last_sent_at": settings.get("last_email_digest_sent_at"),
    }


@router.get("/digest/preview")
def digest_preview():
    """Render the current weekly digest as HTML — for the in-app preview iframe."""
    from fastapi.responses import HTMLResponse
    from polling import email_digest
    settings = config.get_settings()
    conn = get_connection()
    try:
        data = email_digest.build_weekly_digest_data(
            conn, days=int(settings.get("email_digest_interval_days", 7) or 7),
            tz_name=settings.get("display_timezone", "UTC"))
    finally:
        conn.close()
    return HTMLResponse(email_digest.render_weekly_digest_html(data))


@router.post("/digest/settings")
def digest_save_settings(body: dict):
    """Persist the weekly-digest config. The SMTP password is auto-vaulted (it's
    in CREDENTIAL_FIELDS); a blank/absent password keeps the stored one. Only
    provided keys are updated."""
    from polling import email_digest
    update = {}
    if "email_digest_enabled" in body:
        update["email_digest_enabled"] = bool(body["email_digest_enabled"])
    if "smtp_use_tls" in body:
        update["smtp_use_tls"] = bool(body["smtp_use_tls"])
    if "email_digest_recipients" in body:
        update["email_digest_recipients"] = str(body["email_digest_recipients"] or "")
    if "smtp_host" in body:
        update["smtp_host"] = str(body["smtp_host"] or "").strip()
    if "smtp_username" in body:
        update["smtp_username"] = str(body["smtp_username"] or "").strip()
    if "smtp_from" in body:
        update["smtp_from"] = str(body["smtp_from"] or "").strip()
    if "email_digest_interval_days" in body:
        try:
            update["email_digest_interval_days"] = max(1, min(90, int(body["email_digest_interval_days"])))
        except (TypeError, ValueError):
            pass
    if "smtp_port" in body:
        try:
            update["smtp_port"] = int(body["smtp_port"])
        except (TypeError, ValueError):
            pass
    # Only overwrite the vaulted password when a non-empty one is supplied.
    if body.get("smtp_password"):
        update["smtp_password"] = str(body["smtp_password"])
    config.save_settings(update)
    settings = config.get_settings()
    return {"ok": True, "recipients": email_digest.parse_recipients(settings),
            "has_password": bool(settings.get("smtp_password"))}


@router.post("/digest/test")
def digest_test():
    """Send a one-off test digest to the configured recipients right now. Bypasses
    the enabled gate and does not reset the weekly schedule."""
    from polling import email_digest
    try:
        result = email_digest.send_weekly_email_digest(force=True)
    except Exception as e:
        raise HTTPException(400, detail=str(e) or "Send failed")
    if not result.get("sent"):
        raise HTTPException(400, detail=f"Not sent: {result.get('reason', 'unknown')}")
    return result


@router.get("/analytics/historical")
def get_historical_analytics(weeks: int = Query(12)):
    """Return historical analytics: best periods, fastest growing, weekly growth."""
    conn = get_connection()
    try:
        weeks = min(52, max(1, weeks))
        result = {
            "best_month": None,
            "fastest_growing": None,
            "weekly_growth": [],
            "milestone_history": [],
        }

        # Best month: find the month with the highest total views gained
        # Each tuple: (platform_key, snap_table, sub_table, views_col, faves_col, comments_col)
        # views_col is None for platforms without a views column (e.g. Itaku)
        table_pairs = [
            ("ib",  "snapshots",       "submissions",       "views", "favorites_count", "comments_count"),
            ("fa",  "fa_snapshots",    "fa_submissions",    "views", "favorites_count", "comments_count"),
            ("ws",  "ws_snapshots",    "ws_submissions",    "views", "favorites_count", "comments_count"),
            ("sf",  "sf_snapshots",    "sf_submissions",    "views", "favorites_count", "comments_count"),
            ("sqw", "sqw_snapshots",   "sqw_submissions",   "views", "favorites_count", "comments_count"),
            ("ao3", "ao3_snapshots",   "ao3_submissions",   "views", "favorites_count", "comments_count"),
            ("da",  "da_snapshots",    "da_submissions",    "views", "favorites_count", "comments_count"),
            ("wp",  "wp_snapshots",    "wp_submissions",    "reads", "votes",           "comments_count"),
            ("ik",  "ik_snapshots",    "ik_submissions",    None,    "likes",           "comments_count"),
        ]

        month_data = {}
        for plat, snap_t, _, v_col, f_col, c_col in table_pairs:
            try:
                # Build column expressions, using 0 for missing columns
                v_expr = f"MAX({v_col}) - MIN({v_col})" if v_col else "0"
                f_expr = f"MAX({f_col}) - MIN({f_col})" if f_col else "0"
                c_expr = f"MAX({c_col}) - MIN({c_col})" if c_col else "0"
                rows = conn.execute(f"""
                    SELECT strftime('%Y-%m', polled_at) as month,
                           {v_expr} as views_delta,
                           {f_expr} as faves_delta,
                           {c_expr} as comments_delta
                    FROM {snap_t}
                    GROUP BY month, submission_id
                """).fetchall()
                for r in rows:
                    m = r["month"]
                    if m not in month_data:
                        month_data[m] = {"month": m, "views": 0, "faves": 0, "comments": 0}
                    month_data[m]["views"] += r["views_delta"] or 0
                    month_data[m]["faves"] += r["faves_delta"] or 0
                    month_data[m]["comments"] += r["comments_delta"] or 0
            except Exception:
                pass

        if month_data:
            months_list = list(month_data.values())
            best_views = max(months_list, key=lambda x: x["views"])
            best_faves = max(months_list, key=lambda x: x["faves"])
            best_comments = max(months_list, key=lambda x: x["comments"])
            result["best_month"] = {
                "views": {"period": best_views["month"], "delta": best_views["views"]},
                "faves": {"period": best_faves["month"], "delta": best_faves["faves"]},
                "comments": {"period": best_comments["month"], "delta": best_comments["comments"]},
            }

        # Fastest growing all-time: top submissions by views gained across platforms
        fastest = []
        for plat, snap_t, sub_t, v_col, f_col, _ in table_pairs:
            if not v_col:
                # Skip platforms without a views column for "fastest growing by views"
                continue
            try:
                rows = conn.execute(f"""
                    SELECT s.submission_id, s.title, s.{v_col} as current_views,
                           s.{f_col} as current_faves,
                           MAX(sn.{v_col}) - MIN(sn.{v_col}) as views_gained,
                           JULIANDAY('now') - JULIANDAY(MIN(sn.polled_at)) as days_tracked
                    FROM {sub_t} s
                    JOIN {snap_t} sn ON s.submission_id = sn.submission_id
                    GROUP BY s.submission_id
                    HAVING views_gained > 0
                    ORDER BY views_gained DESC
                    LIMIT 5
                """).fetchall()
                for row in rows:
                    days = max(1, row["days_tracked"] or 1)
                    fastest.append({
                        "platform": plat.upper(),
                        "title": row["title"],
                        "views": row["current_views"],
                        "faves": row["current_faves"],
                        "views_per_day": (row["views_gained"] or 0) / days,
                    })
            except Exception:
                pass
        fastest.sort(key=lambda x: x["views_per_day"], reverse=True)
        result["fastest_growing"] = fastest[:10]

        # Weekly growth report
        weekly = {}
        for plat, snap_t, _, v_col, f_col, c_col in table_pairs:
            try:
                v_expr = f"MAX({v_col}) - MIN({v_col})" if v_col else "0"
                f_expr = f"MAX({f_col}) - MIN({f_col})" if f_col else "0"
                c_expr = f"MAX({c_col}) - MIN({c_col})" if c_col else "0"
                rows = conn.execute(f"""
                    SELECT strftime('%Y-W%W', polled_at) as week_label,
                           {v_expr} as views_delta,
                           {f_expr} as faves_delta,
                           {c_expr} as comments_delta
                    FROM {snap_t}
                    WHERE polled_at >= datetime('now', ? || ' days')
                    GROUP BY week_label, submission_id
                """, (str(-(weeks * 7)),)).fetchall()
                for r in rows:
                    w = r["week_label"]
                    if w not in weekly:
                        weekly[w] = {"week_label": w, "views_delta": 0, "faves_delta": 0, "comments_delta": 0}
                    weekly[w]["views_delta"] += r["views_delta"] or 0
                    weekly[w]["faves_delta"] += r["faves_delta"] or 0
                    weekly[w]["comments_delta"] += r["comments_delta"] or 0
            except Exception:
                pass
        result["weekly_growth"] = sorted(weekly.values(), key=lambda x: x["week_label"])

        return result
    finally:
        conn.close()
