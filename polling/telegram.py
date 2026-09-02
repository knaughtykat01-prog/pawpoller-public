"""Centralised Telegram notification helpers.

Provides reusable send function and higher-level notification builders:
  - Poll cycle summaries (per-platform)
  - Poll error alerts
  - Milestone alerts (view/fave/comment thresholds)
  - Periodic cross-platform digest reports (configurable interval)
"""

from __future__ import annotations
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import httpx

import config
from database import platform_metrics
from database.db import get_connection
from database.scope import account_clause  # optional `account_id = ?` WHERE-injection

from html import escape as _esc

logger = logging.getLogger(__name__)

# Platform metric columns + table pairs, derived from the canonical registry
# (database/platform_metrics.py) rather than hand-listed here. Both dicts used
# to be maintained by hand and both had silently missed Instagram; the metric
# map also put e621/Furbooru's `score` in the *views* slot.
#
# `score` now has its own key. The milestone scan below still measures the
# booru family against the view thresholds (unchanged behaviour — see
# check_milestones_for_platform), but nothing downstream can mistake a net
# up−down score for a view count any more.
PLATFORM_METRICS = {
    code: {
        "views": platform_metrics.get(code).views,
        "score": platform_metrics.get(code).score,
        "faves": platform_metrics.get(code).faves,
        "comments": platform_metrics.get(code).comments,
    }
    for code in platform_metrics.ALL_CODES
}

# Each platform's (snapshot_table, submission_table) pair — the pairing every
# digest function walks.
PLATFORM_TABLES = {
    code: (platform_metrics.get(code).snapshots, platform_metrics.get(code).table)
    for code in platform_metrics.ALL_CODES
}

# Default milestone thresholds — overridden by settings.json if configured.
_DEFAULT_VIEW_MILESTONES = [100, 250, 500, 1000, 2500, 5000, 10000, 25000, 50000, 100000]
_DEFAULT_FAVE_MILESTONES = [10, 25, 50, 100, 250, 500, 1000, 2500, 5000]
_DEFAULT_COMMENT_MILESTONES = [10, 25, 50, 100, 250, 500, 1000]


def _get_milestones() -> dict:
    """Return current milestone thresholds from settings, falling back to defaults."""
    s = config.get_settings()
    return {
        "views": s.get("milestone_views", _DEFAULT_VIEW_MILESTONES),
        "faves": s.get("milestone_faves", _DEFAULT_FAVE_MILESTONES),
        "comments": s.get("milestone_comments", _DEFAULT_COMMENT_MILESTONES),
    }


def format_tz(dt: datetime | None = None) -> str:
    """Format a datetime in the user's configured display_timezone.

    If *dt* is None, uses the current UTC time.  Falls back to UTC if the
    configured timezone is invalid.  Returns 'YYYY-MM-DD HH:MM TZ'.
    """
    if dt is None:
        dt = datetime.now(timezone.utc)
    tz_name = config.get_settings().get("display_timezone", "UTC")
    try:
        tz = ZoneInfo(tz_name)
    except (KeyError, Exception):
        tz = timezone.utc
    local = dt.astimezone(tz)
    abbr = local.strftime("%Z")
    return f"{local.strftime('%Y-%m-%d %H:%M')} {abbr}"


# ── Core send helper ─────────────────────────────────────────

async def send_telegram(text: str) -> bool:
    """Send an HTML-formatted Telegram message.  Returns True on success."""
    settings = config.get_settings()
    if not settings.get("telegram_enabled", False):
        return False
    token = settings.get("telegram_bot_token")
    chat_id = settings.get("telegram_chat_id")
    if not token or not chat_id:
        return False
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            )
        return True
    except Exception as e:
        logger.warning("Failed to send Telegram message: %s", e)
        return False


# ── Poll cycle summary ───────────────────────────────────────

PLATFORM_EMOJI = {"ib": "🐾", "fa": "🦊", "ws": "🦎", "sf": "🐺", "sqw": "🦑", "ao3": "📖", "da": "🎨", "wp": "📙", "ik": "🎯", "bsky": "🦋", "tw": "🐦", "mast": "🐘", "tum": "📘", "pix": "🖌", "thr": "🧵", "ig": "📷", "e621": "🐾", "fn": "🌐", "fbr": "🖼"}
PLATFORM_NAME = {"ib": "Inkbunny", "fa": "FurAffinity", "ws": "Weasyl", "sf": "SoFurry", "sqw": "SquidgeWorld", "ao3": "AO3", "da": "DeviantArt", "wp": "Wattpad", "ik": "Itaku", "bsky": "Bluesky", "tw": "X/Twitter", "mast": "Mastodon", "tum": "Tumblr", "pix": "Pixiv", "thr": "Threads", "ig": "Instagram", "e621": "e621", "fn": "FurryNetwork", "fbr": "Furbooru"}


# ── Persona / account context (multi-account notification labelling) ─────────
# These let per-cycle alerts + milestones prefix a header with which account (and
# persona) the activity belongs to — but ONLY when it would be ambiguous. On the
# common single-account-per-platform install nothing changes (prefix is "").

def _account_context(conn, account_id: int | None) -> tuple[str | None, str | None]:
    """Return ``(account_label, persona_name|None)`` for *account_id*.

    ``(None, None)`` if the account can't be resolved (e.g. legacy None id).
    """
    if account_id is None:
        return None, None
    try:
        from database import accounts as accounts_db, personas as personas_db
        acct = accounts_db.get_account(conn, account_id)
        if not acct:
            return None, None
        label = acct.get("label") or PLATFORM_NAME.get(acct.get("platform", ""), "")
        persona_name = None
        pid = acct.get("persona_id")
        if pid is not None:
            p = personas_db.get_persona(conn, pid)
            persona_name = p["name"] if p else None
        return label, persona_name
    except Exception:
        return None, None


def _should_label_account(conn, platform: str, account_id: int | None) -> bool:
    """True when an alert for this account needs a disambiguating prefix.

    That's when the platform has >1 enabled account OR the account belongs to a
    persona. Single unassigned-account installs get no prefix (unchanged output).
    """
    if account_id is None:
        return False
    try:
        from database import accounts as accounts_db
        _, persona_name = _account_context(conn, account_id)
        if persona_name:
            return True
        enabled = [a for a in accounts_db.list_accounts(conn, platform) if a.get("enabled")]
        return len(enabled) > 1
    except Exception:
        return False


def _account_who(conn, platform: str, account_id: int | None) -> str:
    """Escaped ``"Persona · Label"`` (or ``"Label"``) identifying an account, or
    ``""`` when no disambiguation is needed (single unassigned-account install)."""
    if not _should_label_account(conn, platform, account_id):
        return ""
    label, persona_name = _account_context(conn, account_id)
    if not label:
        return ""
    who = f"{persona_name} · {label}" if persona_name else label
    return _esc(who)


def _account_header_prefix(conn, platform: str, account_id: int | None) -> str:
    """A ``"Who — "`` header prefix for per-cycle activity alerts, or ``""``."""
    who = _account_who(conn, platform, account_id)
    return f"{who} — " if who else ""


def account_alert_prefix(platform: str, account_id: int | None) -> str:
    """Public, connection-managing wrapper around :func:`_account_header_prefix`.

    Pollers call this to get a ``"Persona · Account — "`` prefix (already escaped)
    to splice into an instant-alert header, or ``""`` when the platform has a
    single unassigned account. Opens + closes its own short-lived connection.
    """
    try:
        conn = get_connection()
        try:
            return _account_header_prefix(conn, platform, account_id)
        finally:
            conn.close()
    except Exception:
        return ""

# When True, individual per-platform summaries and error alerts are
# suppressed.  Set by the unified poll orchestrator in server.py so it
# can send ONE consolidated message instead of 7+ individual ones.
# Manual /poll commands leave this False so you still get per-platform output.
orchestrated_poll_active = False


async def send_poll_summary(platform: str, stats: dict, duration: float) -> None:
    """Send a compact poll cycle summary for a single platform.

    Suppressed during orchestrated polls (server.py sends a consolidated
    summary instead).  Still fires for manual /poll commands.
    """
    if orchestrated_poll_active:
        return
    settings = config.get_settings()
    if not settings.get("telegram_poll_summaries", True):
        return

    emoji = PLATFORM_EMOJI.get(platform, "")
    name = PLATFORM_NAME.get(platform, platform.upper())
    subs = stats.get("submissions_found", 0)
    snaps = stats.get("snapshots_inserted", 0)

    lines = [f"<b>{emoji} {name} Poll Complete</b>"]
    lines.append(f"  {subs} submissions, {snaps} snapshots in {duration:.1f}s")

    new_faves = stats.get("new_faves_found", 0)
    new_comments = stats.get("new_comments_found", 0)
    new_watchers = stats.get("new_watchers_found", 0)
    activity = []
    if new_faves:
        activity.append(f"+{new_faves} fave{'s' if new_faves != 1 else ''}")
    if new_comments:
        activity.append(f"+{new_comments} comment{'s' if new_comments != 1 else ''}")
    if new_watchers:
        activity.append(f"+{new_watchers} watcher{'s' if new_watchers != 1 else ''}")
    if activity:
        lines.append(f"  New: {', '.join(activity)}")

    await send_telegram("\n".join(lines))


# ── Poll error classification ───────────────────────────────

_ERROR_PATTERNS: list[tuple[str, str, str]] = [
    # (substring to match, short label, hint)
    ("login failed", "Login blocked", "Likely Cloudflare/rate-limit, not bad creds"),
    ("Shields are up", "Cloudflare challenge", "AO3 is blocking automated access"),
    ("429 Too Many Requests", "Rate limited", "Will back off automatically"),
    ("429", "Rate limited", "Platform is throttling requests"),
    ("403 Forbidden", "Access denied", "Platform may be blocking datacenter IPs"),
    ("403", "Blocked", "May need proxy or updated cookies"),
    ("404 Not Found", "Not found", "API endpoint may have changed"),
    ("check credentials", "Auth issue", "Verify creds in dashboard if this persists"),
    ("timeout", "Timed out", "Platform may be slow or unreachable"),
    ("ConnectError", "Connection failed", "Platform may be down"),
    ("ConnectTimeout", "Connection timed out", "Platform unreachable"),
    ("RemoteProtocolError", "Connection dropped", "Platform closed the connection"),
    ("SSL", "SSL error", "Certificate or TLS issue"),
]


def _classify_error(error_str: str) -> tuple[str, str]:
    """Map a raw error string to a user-friendly (label, hint) pair."""
    lower = error_str.lower()
    for pattern, label, hint in _ERROR_PATTERNS:
        if pattern.lower() in lower:
            return label, hint
    return "Error", ""


def _format_error_for_telegram(platform: str, error_str: str) -> str:
    """Build a user-friendly error line for Telegram messages."""
    label, hint = _classify_error(error_str)
    name = PLATFORM_NAME.get(platform, platform.upper())
    emoji = PLATFORM_EMOJI.get(platform, "")
    line = f"❌ {emoji} {name}: {_esc(label)}"
    if hint:
        line += f"\n     <i>{_esc(hint)}</i>"
    return line


# ── Poll error alert ─────────────────────────────────────────

async def send_poll_error(platform: str, error: Exception) -> None:
    """Send an alert when a poll cycle fails.

    Suppressed during orchestrated polls — errors are included in the
    consolidated summary instead.
    """
    if orchestrated_poll_active:
        return
    settings = config.get_settings()
    if not settings.get("telegram_error_alerts", True):
        return

    emoji = PLATFORM_EMOJI.get(platform, "")
    name = PLATFORM_NAME.get(platform, platform.upper())
    from polling.notifications import describe_error
    error_str = describe_error(error)[:200]
    label, hint = _classify_error(error_str)
    lines = [f"<b>{emoji} {name} Poll Failed</b>"]
    lines.append(f"  {_esc(label)}")
    if hint:
        lines.append(f"  <i>{_esc(hint)}</i>")
    lines.append(f"  <code>{_esc(error_str[:120])}</code>")
    await send_telegram("\n".join(lines))


# ── Consolidated poll summary (used by orchestrator) ─────────

async def send_consolidated_poll_summary(results: list[dict], duration: float) -> None:
    """Send ONE summary message for an orchestrated poll cycle.

    *results* is a list of dicts, each with:
      - platform: short code (e.g. "ib")
      - stats: poll stats dict (on success), OR
      - error: error message string (on failure)

    Format:
      All OK  → "✅ All 6 Polls Complete (25s) ..."
      Partial → "⚠️ 5/6 Polls Complete (18s) ... ❌ FA: error"
    """
    settings = config.get_settings()
    if not settings.get("telegram_poll_summaries", True):
        return
    if not results:
        return

    ok = [r for r in results if "stats" in r]
    failed = [r for r in results if "error" in r]

    if not failed:
        header = f"✅ All {len(ok)} Polls Complete ({duration:.0f}s)"
    else:
        header = f"⚠️ {len(ok)}/{len(results)} Polls Complete ({duration:.0f}s)"

    lines = [f"<b>{header}</b>"]

    def _plat_parts(group: list) -> list:
        """Platform count parts (e.g. "🐾 9"), labelling accounts when a platform
        has more than one in this group."""
        counts: dict[str, int] = {}
        for r in group:
            counts[r["platform"]] = counts.get(r["platform"], 0) + 1
        parts = []
        for r in group:
            emoji = PLATFORM_EMOJI.get(r["platform"], "")
            subs = r["stats"].get("submissions_found", 0)
            if counts.get(r["platform"], 0) > 1 and r.get("label"):
                parts.append(f"{emoji} {_esc(r['label'])}: {subs}")
            else:
                parts.append(f"{emoji}{subs}")
        return parts

    def _activity(group: list) -> list:
        tf = sum(r["stats"].get("new_faves_found", 0) for r in group)
        tc = sum(r["stats"].get("new_comments_found", 0) for r in group)
        tw = sum(r["stats"].get("new_watchers_found", 0) for r in group)
        act = []
        if tf:
            act.append(f"+{tf} fave{'s' if tf != 1 else ''}")
        if tc:
            act.append(f"+{tc} comment{'s' if tc != 1 else ''}")
        if tw:
            act.append(f"+{tw} watcher{'s' if tw != 1 else ''}")
        return act

    # Resolve each ok result's persona via its account_id (only if personas exist).
    conn = get_connection()
    try:
        from database import personas as personas_db
        personas_defined = bool(personas_db.list_personas(conn))
        pmap: dict = {}
        if personas_defined:
            for pid_key, accts in personas_db.list_accounts_by_persona(conn).items():
                pname = None
                if pid_key is not None:
                    p = personas_db.get_persona(conn, pid_key)
                    pname = p["name"] if p else None
                for a in accts:
                    pmap[a["account_id"]] = (pid_key, pname)
    finally:
        conn.close()

    if not personas_defined:
        # No personas → flat summary (unchanged single-user behaviour).
        parts = _plat_parts(ok)
        if parts:
            lines.append("  " + "  ".join(parts))
        act = _activity(ok)
        if act:
            lines.append(f"  {', '.join(act)}")
    else:
        # Group the cycle's results by persona (Unassigned last) so each identity
        # gets its own sub-section instead of one lumped line.
        groups: dict = {}
        for r in ok:
            pid, pname = pmap.get(r.get("account_id"), (None, None))
            groups.setdefault((pid, pname), []).append(r)
        for (pid, pname), grp in sorted(groups.items(), key=lambda it: (it[0][0] is None, it[0][1] or "")):
            label = pname if pid is not None else "Unassigned"
            lines.append(f"<b>👤 {_esc(label)}</b>")
            parts = _plat_parts(grp)
            if parts:
                lines.append("  " + "  ".join(parts))
            act = _activity(grp)
            if act:
                lines.append(f"  {', '.join(act)}")

    # Failed platforms — classified error messages
    for r in failed:
        lines.append(_format_error_for_telegram(r["platform"], r["error"]))

    await send_telegram("\n".join(lines))


# ── Milestone alerts ─────────────────────────────────────────

def _crossed_milestone(current: int, previous: int, milestones: list[int]) -> int | None:
    """Return the highest milestone crossed between previous and current, or None."""
    crossed = None
    for m in milestones:
        if previous < m <= current:
            crossed = m
    return crossed


async def check_milestones(platform: str, submission_id: int, title: str,
                           current_views: int, current_faves: int, current_comments: int,
                           prev_views: int, prev_faves: int, prev_comments: int,
                           who: str = "") -> None:
    """Check if a submission crossed any milestone thresholds and notify.

    *who* is an already-escaped "Persona · Account" label appended to the header
    when the platform has multiple accounts (empty on single-account installs).
    """
    settings = config.get_settings()
    if not settings.get("telegram_milestones", True):
        return

    emoji = PLATFORM_EMOJI.get(platform, "")
    name = PLATFORM_NAME.get(platform, platform.upper())
    lines = []

    ms = _get_milestones()
    view_m = _crossed_milestone(current_views, prev_views, ms["views"])
    if view_m:
        lines.append(f"  👁 {current_views:,} views (passed {view_m:,})")

    fave_m = _crossed_milestone(current_faves, prev_faves, ms["faves"])
    if fave_m:
        lines.append(f"  ❤️ {current_faves:,} faves (passed {fave_m:,})")

    comment_m = _crossed_milestone(current_comments, prev_comments, ms["comments"])
    if comment_m:
        lines.append(f"  💬 {current_comments:,} comments (passed {comment_m:,})")

    if lines:
        suffix = f" · {who}" if who else ""
        header = f"<b>{emoji} {name} Milestone!{suffix}</b>\n<b>{_esc(title)}</b>"
        await send_telegram(header + "\n" + "\n".join(lines))


async def check_milestones_batch(platform: str, snap_table: str, sub_table: str,
                                 account_id: int | None = None) -> None:
    """Compare latest two snapshots for all submissions on a platform and fire milestone alerts.

    Called at the end of each poll cycle.  Compares the two most recent snapshots
    per submission to detect threshold crossings.
    Uses platform-aware column names via PLATFORM_METRICS. With *account_id* set,
    only that account's submissions are scanned — both to label the alert and to
    avoid double-firing when a platform has multiple accounts (each account's poll
    only checks its own submissions).
    """
    settings = config.get_settings()
    if not settings.get("telegram_milestones", True):
        return

    metrics = PLATFORM_METRICS.get(platform, PLATFORM_METRICS["ib"])
    # Booru platforms have no view counter; their headline number is the net
    # score. Measuring it against the view thresholds is what has always
    # happened here — kept deliberately so e621/Furbooru alerts don't silently
    # stop. (Follow-up: give score its own thresholds + wording.)
    views_col = metrics["views"] or metrics.get("score")
    faves_col = metrics["faves"]
    comments_col = metrics["comments"]

    # Build SELECT columns, skipping None (e.g. Itaku has no views)
    select_cols = []
    if views_col:
        select_cols.append(views_col)
    if faves_col:
        select_cols.append(faves_col)
    if comments_col:
        select_cols.append(comments_col)

    if not select_cols:
        return

    conn = get_connection()
    try:
        who = _account_who(conn, platform, account_id)
        where, wparams = account_clause(account_id)
        subs = conn.execute(
            f"SELECT submission_id, title FROM {sub_table}" + (f" WHERE {where}" if where else ""),
            wparams,
        ).fetchall()
        for sub in subs:
            sid = sub["submission_id"]
            rows = conn.execute(
                f"SELECT {', '.join(select_cols)} FROM {snap_table} "
                f"WHERE submission_id = ? ORDER BY polled_at DESC LIMIT 2",
                (sid,),
            ).fetchall()
            if len(rows) < 2:
                continue
            curr, prev = dict(rows[0]), dict(rows[1])
            await check_milestones(
                platform, sid, sub["title"],
                curr.get(views_col, 0) if views_col else 0,
                curr.get(faves_col, 0) if faves_col else 0,
                curr.get(comments_col, 0) if comments_col else 0,
                prev.get(views_col, 0) if views_col else 0,
                prev.get(faves_col, 0) if faves_col else 0,
                prev.get(comments_col, 0) if comments_col else 0,
                who=who,
            )
    finally:
        conn.close()


# ── Goal Completion Check ─────────────────────────────────────

async def check_goals() -> None:
    """Check all active goals and send notifications for newly completed ones.

    Suppressed during orchestrated polls — the orchestrator calls this once
    after all platforms finish so we avoid 11 redundant DB scans.
    """
    if orchestrated_poll_active:
        return
    settings = config.get_settings()
    if not settings.get("telegram_enabled", False):
        return

    # Use the shared whitelist from config — single source of truth for
    # valid metric column names that are safe to interpolate into SQL.
    ALLOWED_METRICS = config.ALLOWED_GOAL_METRICS
    conn = get_connection()
    try:
        goals = conn.execute("SELECT * FROM goals WHERE completed_at IS NULL").fetchall()
        # Registry-derived, so a goal set on a newly wired platform resolves.
        table_map = {code: platform_metrics.table_for(code)
                     for code in platform_metrics.ALL_CODES}

        for g in goals:
            g = dict(g)
            metric = g["metric"]
            if metric not in ALLOWED_METRICS:
                continue
            current = 0
            title = None

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
                    for plat_key, tbl in table_map.items():
                        try:
                            r = conn.execute(f"SELECT COALESCE(SUM({metric}), 0) as total FROM {tbl}").fetchone()
                            current += r["total"]
                        except Exception:
                            # Column doesn't exist on this platform — skip
                            pass
                else:
                    table = table_map.get(g["platform"])
                    if table:
                        try:
                            r = conn.execute(f"SELECT COALESCE(SUM({metric}), 0) as total FROM {table}").fetchone()
                            current = r["total"]
                        except Exception:
                            pass

            if current >= g["target_value"] and g["target_value"] > 0:
                # Use rowcount to prevent duplicate notifications from concurrent pollers
                cursor = conn.execute(
                    "UPDATE goals SET completed_at = datetime('now') WHERE goal_id = ? AND completed_at IS NULL",
                    (g["goal_id"],),
                )
                conn.commit()
                if cursor.rowcount == 0:
                    continue
                emoji = PLATFORM_EMOJI.get(g["platform"], "🎯")
                metric_labels = {
                    "views": "views", "favorites_count": "faves", "comments_count": "comments",
                    "reads": "reads", "votes": "votes", "likes": "likes",
                    "reshares": "reshares", "downloads": "downloads", "num_lists": "lists",
                }
                metric_label = metric_labels.get(metric, metric)
                sub_label = f"\n<b>{_esc(title)}</b>" if title else ""
                await send_telegram(
                    f"<b>{emoji} Goal Reached!</b>{sub_label}\n"
                    f"  🎯 {current:,} / {g['target_value']:,} {metric_label}"
                )
    finally:
        conn.close()


# ── Periodic Digest Report ───────────────────────────────────

def _get_digest_deltas(conn, snap_table: str, sub_table: str, platform: str, hours: int = 6, account_id: int | None = None) -> dict:
    """Compute aggregate stat deltas over the last *hours* for a platform.

    Returns dict with total_views_delta, total_faves_delta, total_comments_delta,
    top_gainers (up to 3 submissions with biggest view/fave gains), and submission count.
    Uses platform-aware column names via PLATFORM_METRICS. With *account_id* set,
    only that account's submissions are counted (the outer ``s`` table is scoped;
    the snapshot subquery stays unscoped since submission_ids are account-unique).
    """
    metrics = PLATFORM_METRICS.get(platform, PLATFORM_METRICS["ib"])
    # Booru platforms have no view counter; their headline number is the net
    # score. Measuring it against the view thresholds is what has always
    # happened here — kept deliberately so e621/Furbooru alerts don't silently
    # stop. (Follow-up: give score its own thresholds + wording.)
    views_col = metrics["views"] or metrics.get("score")
    faves_col = metrics["faves"]
    comments_col = metrics["comments"]

    # Build dynamic SELECT columns for the current sub table
    current_cols = ["s.submission_id", "s.title"]
    old_select_cols = []  # columns from the old snapshot subquery
    old_join_cols = []    # columns to select in the inner snapshot subquery

    if views_col:
        current_cols.append(f"s.{views_col}")
        old_select_cols.append(f"s1.{views_col} as old_views")
        old_join_cols.append(f"s1.{views_col}")
    if faves_col:
        current_cols.append(f"s.{faves_col}")
        old_select_cols.append(f"s1.{faves_col} as old_faves")
        old_join_cols.append(f"s1.{faves_col}")
    if comments_col:
        current_cols.append(f"s.{comments_col}")
        old_select_cols.append(f"s1.{comments_col} as old_comments")
        old_join_cols.append(f"s1.{comments_col}")

    old_cols_str = ", ".join(old_select_cols) if old_select_cols else "1 as _dummy"

    acc_sql, acc_params = account_clause(account_id, "s")
    rows = conn.execute(
        f"""SELECT {', '.join(current_cols)},
                   {', '.join(f'old.{c.split(" as ")[1]}' for c in old_select_cols) if old_select_cols else '1 as _dummy2'}
            FROM {sub_table} s
            LEFT JOIN (
                SELECT s1.submission_id, {old_cols_str}
                FROM {snap_table} s1
                INNER JOIN (
                    SELECT submission_id, MAX(polled_at) as max_polled
                    FROM {snap_table}
                    WHERE polled_at <= datetime('now', '-' || ? || ' hours')
                    GROUP BY submission_id
                ) s2 ON s1.submission_id = s2.submission_id AND s1.polled_at = s2.max_polled
            ) old ON s.submission_id = old.submission_id"""
        + (f" WHERE {acc_sql}" if acc_sql else ""),
        [str(hours)] + acc_params,
    ).fetchall()

    total_views_delta = 0
    total_faves_delta = 0
    total_comments_delta = 0
    gainers = []

    for r in rows:
        r = dict(r)
        old_v = r.get("old_views") or 0
        old_f = r.get("old_faves") or 0
        old_c = r.get("old_comments") or 0
        dv = (r.get(views_col, 0) or 0) - old_v if views_col else 0
        df = (r.get(faves_col, 0) or 0) - old_f if faves_col else 0
        dc = (r.get(comments_col, 0) or 0) - old_c if comments_col else 0
        total_views_delta += max(dv, 0)
        total_faves_delta += max(df, 0)
        total_comments_delta += max(dc, 0)
        if dv > 0 or df > 0:
            gainers.append({"title": r["title"], "views": dv, "faves": df, "comments": dc})

    gainers.sort(key=lambda x: x["views"], reverse=True)

    return {
        "submissions": len(rows),
        "views_delta": total_views_delta,
        "faves_delta": total_faves_delta,
        "comments_delta": total_comments_delta,
        "top_gainers": gainers[:3],
    }


def _get_platform_totals(conn, sub_table: str, platform: str, account_id: int | None = None) -> dict:
    """Get current aggregate totals for a platform.

    Uses platform-aware column names via PLATFORM_METRICS. With *account_id* set,
    the totals are scoped to that one account (for per-persona digests).
    """
    metrics = PLATFORM_METRICS.get(platform, PLATFORM_METRICS["ib"])
    # Booru platforms have no view counter; their headline number is the net
    # score. Measuring it against the view thresholds is what has always
    # happened here — kept deliberately so e621/Furbooru alerts don't silently
    # stop. (Follow-up: give score its own thresholds + wording.)
    views_col = metrics["views"] or metrics.get("score")
    faves_col = metrics["faves"]
    comments_col = metrics["comments"]

    views_expr = f"COALESCE(SUM({views_col}),0)" if views_col else "0"
    faves_expr = f"COALESCE(SUM({faves_col}),0)" if faves_col else "0"
    comments_expr = f"COALESCE(SUM({comments_col}),0)" if comments_col else "0"

    where, params = account_clause(account_id)
    row = conn.execute(
        f"SELECT COUNT(*) as subs, {views_expr} as views, "
        f"{faves_expr} as faves, "
        f"{comments_expr} as comments "
        f"FROM {sub_table}" + (f" WHERE {where}" if where else ""),
        params,
    ).fetchone()
    return dict(row)


def _ordered_digest_units(conn, hours: int) -> list[tuple[str, list, bool]]:
    """Resolve the (title, accounts, legacy) digest units to send.

    - No personas defined → ONE unit with the original "PawPoller N-Hour Digest"
      title covering every enabled account (back-compat; the per-account breakdown
      naturally separates multiple accounts on a platform).
    - Personas defined → one unit per persona (in sort order), plus a trailing
      "Unassigned" unit for any accounts without a persona.
    """
    from database import personas as personas_db
    groups = personas_db.list_accounts_by_persona(conn, enabled_only=True)
    personas = personas_db.list_personas(conn)
    if not personas:
        all_accts = [a for accts in groups.values() for a in accts]
        return [(f"PawPoller {hours}-Hour Digest", all_accts, True)]
    units: list[tuple[str, list, bool]] = []
    for p in personas:
        units.append((p["name"], groups.get(p["persona_id"], []), False))
    if groups.get(None):
        units.append(("Unassigned", groups[None], False))
    return units


def _persona_account_lines(conn, accts: list, hours: int, include_watchers: bool = False,
                           only_changed: bool = False):
    """Build the per-account digest breakdown for one digest unit's accounts.

    Returns ``(lines, combined, gainers, had_data)`` where *combined* has delta
    keys views/faves/comments + current-total keys t_views/t_faves/t_comments,
    and *gainers* carries each top-gainer plus its platform emoji.
    """
    lines: list[str] = []
    combined = {"views": 0, "faves": 0, "comments": 0, "t_views": 0, "t_faves": 0, "t_comments": 0}
    gainers: list[dict] = []
    had_data = False

    def _order(a):
        plat = a.get("platform", "")
        idx = list(PLATFORM_TABLES).index(plat) if plat in PLATFORM_TABLES else 99
        return (idx, a.get("sort_order", 0), a.get("account_id", 0))

    for a in sorted(accts, key=_order):
        plat = a.get("platform")
        if plat not in PLATFORM_TABLES:
            continue
        snap_t, sub_t = PLATFORM_TABLES[plat]
        aid = a["account_id"]
        try:
            cnt = conn.execute(f"SELECT COUNT(*) as c FROM {sub_t} WHERE account_id = ?", (aid,)).fetchone()["c"]
        except Exception:
            continue
        if not cnt:
            continue
        totals = _get_platform_totals(conn, sub_t, plat, account_id=aid)
        deltas = _get_digest_deltas(conn, snap_t, sub_t, plat, hours, account_id=aid)
        watcher = _get_watcher_stats(conn, plat, account_id=aid) if include_watchers else None
        # only_changed (the periodic digest): an account with nothing new this
        # window is noise — skip it entirely so the digest only lists platforms
        # that actually moved. Watchers count as movement where they're shown.
        # The weekly digest passes only_changed=False and always lists every
        # account, changed or not.
        if only_changed and not (deltas["views_delta"] or deltas["faves_delta"]
                                 or deltas["comments_delta"] or (watcher or {}).get("new", 0)):
            continue
        had_data = True
        emoji = PLATFORM_EMOJI[plat]
        acct_label = a.get("label") or PLATFORM_NAME[plat]
        lines.append(f"<b>{emoji} {_esc(acct_label)}</b> ({totals['subs']} subs)")
        lines.append(
            f"  Views: {totals['views']:,} (+{deltas['views_delta']:,})"
            f"  Faves: {totals['faves']:,} (+{deltas['faves_delta']:,})"
        )
        lines.append(f"  Comments: {totals['comments']:,} (+{deltas['comments_delta']:,})")
        if watcher:
            wlabel = "Followers" if plat == "sf" else "Watchers"
            lines.append(f"  {wlabel}: {watcher['total']:,} (+{watcher['new']:,})")
        combined["views"] += deltas["views_delta"]
        combined["faves"] += deltas["faves_delta"]
        combined["comments"] += deltas["comments_delta"]
        combined["t_views"] += totals["views"]
        combined["t_faves"] += totals["faves"]
        combined["t_comments"] += totals["comments"]
        for g in deltas["top_gainers"]:
            gainers.append({**g, "emoji": emoji})
        lines.append("")

    return lines, combined, gainers, had_data


def _append_top_gainers(lines: list, gainers: list, limit: int = 3) -> None:
    """Append up to *limit* cross-account top-gainer lines to *lines*."""
    for g in sorted(gainers, key=lambda x: x["views"], reverse=True)[:limit]:
        parts = []
        if g["views"] > 0:
            parts.append(f"+{g['views']} views")
        if g["faves"] > 0:
            parts.append(f"+{g['faves']} faves")
        if parts:
            lines.append(f"  🔥 {g.get('emoji', '')} {_esc(g['title'][:30])}: {', '.join(parts)}")


async def send_digest_report() -> None:
    """Build and send the periodic digest — ONE message per persona.

    Each persona's accounts (across every platform) are summarised with a
    per-account breakdown + persona-combined totals + top gainers. Accounts
    without a persona land in a trailing "Unassigned" digest. Installs with no
    personas defined get a single combined digest (original behaviour).
    """
    settings = config.get_settings()
    if not settings.get("telegram_enabled", False):
        return
    if not settings.get("telegram_digest", True):
        return

    digest_hours = settings.get("telegram_digest_interval_hours", 6)

    conn = get_connection()
    try:
        any_sent = False
        for title, accts, legacy in _ordered_digest_units(conn, digest_hours):
            body, combined, gainers, had_data = _persona_account_lines(
                conn, accts, digest_hours, only_changed=True)
            if not had_data:
                continue
            header = (f"<b>📊 {title}</b>" if legacy
                      else f"<b>📊 {_esc(title)} · {digest_hours}h Digest</b>")
            lines = [header, f"<i>{format_tz()}</i>", ""]
            lines += body
            lines.append("<b>📈 Combined</b>")
            lines.append(
                f"  Views: {combined['t_views']:,} (+{combined['views']:,})"
                f"  Faves: {combined['t_faves']:,} (+{combined['faves']:,})"
            )
            lines.append(f"  Comments: {combined['t_comments']:,} (+{combined['comments']:,})")
            _append_top_gainers(lines, gainers)
            await send_telegram("\n".join(lines))
            any_sent = True

        # Persist timestamp so restarts don't re-send within the window (only
        # when something actually went out — empty cycles stay retryable).
        if any_sent:
            config.save_settings({"last_digest_sent_at": datetime.now(timezone.utc).isoformat()})

        # Piggyback: send FA watcher daily digest if in "daily" mode
        try:
            from polling.fa_poller import send_fa_watcher_digest
            await send_fa_watcher_digest()
        except Exception as e:
            logger.warning("FA watcher digest failed: %s", e)

    finally:
        conn.close()


# ── Weekly Digest Report ────────────────────────────────────

# Watcher/follower tables per platform (table_name, count_filter).
# Only platforms with watcher tracking are listed.
_WATCHER_TABLES = {
    "ib":  ("watchers", "1=1"),
    "fa":  ("fa_watchers", "confirmed=1 AND is_spam=0"),
    "sf":  ("sf_watchers", "1=1"),
}


def _get_watcher_stats(conn, platform: str, days: int = 7, account_id: int | None = None) -> dict | None:
    """Return total and new watcher/follower counts for a platform.

    Returns None if the platform has no watcher table or no data. With
    *account_id* set, scopes to that account's watchers (all watcher tables
    carry account_id; the try/except degrades gracefully if one doesn't).
    """
    entry = _WATCHER_TABLES.get(platform)
    if not entry:
        return None
    table, where = entry
    acc_sql, acc_params = account_clause(account_id)
    acc_and = f" AND {acc_sql}" if acc_sql else ""
    try:
        total = conn.execute(
            f"SELECT COUNT(*) as c FROM {table} WHERE {where}{acc_and}",
            acc_params,
        ).fetchone()["c"]
        new = conn.execute(
            f"SELECT COUNT(*) as c FROM {table} "
            f"WHERE {where}{acc_and} AND first_seen_at >= datetime('now', '-' || ? || ' days')",
            acc_params + [str(days)],
        ).fetchone()["c"]
        return {"total": total, "new": new}
    except Exception:
        return None


async def send_weekly_digest_report() -> None:
    """Build and send the weekly cross-platform digest report.

    Uses 7-day deltas, includes watcher/follower counts, and shows the
    top 5 gainers across all platforms.  Stores ``last_weekly_digest_sent_at``
    to prevent duplicates across restarts and manual triggers.
    """
    settings = config.get_settings()
    if not settings.get("telegram_enabled", False):
        return
    if not settings.get("telegram_weekly_digest", True):
        return

    conn = get_connection()
    try:
        now = datetime.now(timezone.utc)
        # Build "week of" header in the user's display timezone
        tz_name = settings.get("display_timezone", "UTC")
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = timezone.utc
        local_now = now.astimezone(tz)
        from datetime import timedelta
        week_start = (local_now - timedelta(days=7)).strftime("%b %d")
        week_end = local_now.strftime("%b %d, %Y")

        any_sent = False
        for title, accts, legacy in _ordered_digest_units(conn, 168):
            body, combined, gainers, had_data = _persona_account_lines(
                conn, accts, hours=168, include_watchers=True)
            if not had_data:
                continue
            head = "📅 PawPoller Weekly Digest" if legacy else f"📅 {_esc(title)} · Weekly"
            lines = [f"<b>{head}</b>", f"<i>Week of {week_start} — {week_end}</i>", ""]
            lines += body

            # Top 5 gainers across this unit's accounts (weighting faves heavier)
            top = sorted(gainers, key=lambda x: x["views"] + x["faves"] * 10, reverse=True)[:5]
            if top:
                lines.append("<b>🏆 Top Gainers This Week</b>")
                for g in top:
                    parts = []
                    if g["views"] > 0:
                        parts.append(f"+{g['views']:,} views")
                    if g["faves"] > 0:
                        parts.append(f"+{g['faves']:,} faves")
                    if parts:
                        lines.append(f"  {g.get('emoji', '')} {_esc(g['title'][:35])}: {', '.join(parts)}")
                lines.append("")

            lines.append("<b>📈 Weekly Combined</b>")
            lines.append(
                f"  Views: {combined['t_views']:,} (+{combined['views']:,})"
                f"  Faves: {combined['t_faves']:,} (+{combined['faves']:,})"
            )
            lines.append(f"  Comments: {combined['t_comments']:,} (+{combined['comments']:,})")
            await send_telegram("\n".join(lines))
            any_sent = True

        if any_sent:
            config.save_settings({"last_weekly_digest_sent_at": datetime.now(timezone.utc).isoformat()})

    finally:
        conn.close()
