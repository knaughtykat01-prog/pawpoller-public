"""Shared Windows-toast and Telegram notification primitives for pollers.

Each platform poller has its own filter logic — per-platform "notifications
enabled" toggles, comments-only mode, fave-thresholds — but the mechanics
of actually firing a toast or a Telegram message are identical everywhere.
This module captures the mechanics; the filtering stays in each poller.

Three layers, smallest first:

  1. Primitives — ``show_toast`` and ``send_telegram``. Dumb side-effect
     wrappers; both no-op cleanly when their dependency is missing
     (winotify on non-Windows builds; httpx network errors swallowed
     with a warning).

  2. Formatters — ``truncate_with_overflow`` and
     ``format_telegram_summary``. String-builders shared by every
     poller's summary message.

  3. Convenience — ``maybe_show_toast`` and
     ``maybe_send_telegram_summary``. Combine the per-platform settings
     check with the formatter+primitive call in one shot. Most pollers
     can call straight into these and skip the boilerplate.
"""
from __future__ import annotations

import contextvars
import html
import logging
import re
import subprocess
import sys
from typing import Any

# Per-poll-cycle account context for instant-alert labelling. The orchestrator
# (server.py) sets this to ``(platform, account_id)`` before each account's poll
# so maybe_send_telegram_summary can prefix the header with the persona/account —
# but ONLY when that account needs disambiguating. ContextVars are async-safe:
# each gathered platform task gets its own isolated copy, so concurrent polls
# never cross-contaminate. Defaults to no context (no prefix).
current_alert_account: contextvars.ContextVar = contextvars.ContextVar(
    "pp_alert_account", default=(None, None))

import httpx

logger = logging.getLogger(__name__)

TOAST_DEFAULT_VISIBLE = 3
TELEGRAM_DEFAULT_VISIBLE = 5
TELEGRAM_HTTP_TIMEOUT = 10.0
TELEGRAM_MESSAGE_LIMIT = 4096


def describe_error(e: BaseException) -> str:
    """str(e), falling back to the exception type name when the message is empty.

    Timeout-family exceptions (httpx.ReadTimeout, httpcore.ReadTimeout,
    asyncio.TimeoutError, ...) stringify to "" — which produced blank
    "Poll ib failed: " log lines and empty dashboard/Telegram error fields.
    Falling back to the qualified type name makes those self-explanatory
    while leaving exceptions with real messages untouched.
    """
    msg = str(e).strip()
    if msg:
        return msg
    module = type(e).__module__
    name = type(e).__name__
    if module and module not in ("builtins", "__main__"):
        return f"{module}.{name}"
    return name


# ── Formatters ────────────────────────────────────────────────────────

def truncate_with_overflow(items: list[str], max_visible: int) -> list[str]:
    """Return at most ``max_visible`` items, with an "...and N more" tail.

    >>> truncate_with_overflow(["a", "b", "c"], 3)
    ['a', 'b', 'c']
    >>> truncate_with_overflow(["a", "b", "c", "d", "e"], 3)
    ['a', 'b', 'c', '...and 2 more']
    """
    if len(items) <= max_visible:
        return list(items)
    return [*items[:max_visible], f"...and {len(items) - max_visible} more"]


def format_telegram_summary(
    header_html: str,
    items: list[str],
    max_visible: int = TELEGRAM_DEFAULT_VISIBLE,
) -> str:
    """Build a Telegram HTML summary: header + bulleted items + overflow tail.

    Items are rendered as ``  • <item>`` lines (the leading two spaces
    match the existing per-poller formatting). The overflow line uses
    the same truncation rule as ``truncate_with_overflow`` but with the
    Telegram-style indent.
    """
    lines = [header_html]
    for item in items[:max_visible]:
        lines.append(f"  • {item}")
    if len(items) > max_visible:
        lines.append(f"  ...and {len(items) - max_visible} more")
    return "\n".join(lines)


# ── Primitives ────────────────────────────────────────────────────────

def show_toast(
    title: str,
    lines: list[str],
    max_visible: int = TOAST_DEFAULT_VISIBLE,
) -> bool:
    """Fire a desktop toast. Returns True iff actually shown.

    Per-OS backends:
      Windows -- winotify (Windows 10/11 native toast).
      Linux   -- notify-send (libnotify) via subprocess. Available
                 by default on every major desktop environment;
                 silently no-ops if the binary isn't on PATH (e.g.
                 headless server, minimal container).
      macOS / other -- no-op for now (logged at debug level).

    Lazy-imports / shell-outs so server builds can load this module
    without any of these deps present. An empty ``lines`` list is a
    no-op (returns False).
    """
    if not lines:
        return False
    msg = "\n".join(truncate_with_overflow(lines, max_visible))

    if sys.platform == "win32":
        try:
            from winotify import Notification
        except ImportError:
            logger.debug("winotify not available — toast suppressed: %s", title)
            return False
        Notification(app_id="PawPoller", title=title, msg=msg).show()
        return True

    if sys.platform.startswith("linux"):
        # notify-send is the libnotify CLI — present on GNOME, KDE, XFCE,
        # MATE, Cinnamon, etc. by default. --app-name groups our toasts
        # under "PawPoller" in DE notification centres.
        try:
            subprocess.run(
                [
                    "notify-send",
                    "--app-name=PawPoller",
                    "--expire-time=8000",
                    title,
                    msg,
                ],
                check=False,
                timeout=3.0,
            )
            return True
        except FileNotFoundError:
            logger.debug("notify-send not installed — toast suppressed: %s", title)
            return False
        except (subprocess.TimeoutExpired, OSError) as e:
            logger.debug("notify-send failed (%s) — toast suppressed: %s", e, title)
            return False

    logger.debug("Desktop toasts not implemented on %s — suppressed: %s", sys.platform, title)
    return False


async def send_telegram(
    token: str,
    chat_id: str,
    text: str,
    *,
    log_label: str = "notification",
) -> bool:
    """POST a Telegram message via the Bot API. Returns True on success.

    Errors are logged and swallowed (returns False) — notifications are
    best-effort and a transient network failure shouldn't crash the
    poll cycle. The bool return lets callers branch on success when
    they have follow-up state to update (e.g. the FA watcher digest's
    "mark watchers notified" step that must NOT run if delivery failed).

    Telegram answers every send with ``{"ok": …}``; until 4.79.2 the answer was never read, so a message it
    refused (too long, a stray ``<`` in a title, the bot blocked) counted as sent and left no trace (TGSEND).
    """
    if len(text) > TELEGRAM_MESSAGE_LIMIT:
        # Telegram refuses an over-long message outright (400 "message is too long"); cut on a line
        # boundary so no HTML tag is left open (each line closes its own).
        text = text[:TELEGRAM_MESSAGE_LIMIT - 40].rsplit("\n", 1)[0] + "\n\n(trimmed)"
    try:
        async with httpx.AsyncClient(timeout=TELEGRAM_HTTP_TIMEOUT) as client:
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
            resp = await client.post(url, json=payload)
            why = _refusal(resp)
            if why and "parse" in why.lower():
                # Markup Telegram can't read (a title's unescaped "<"): send it as plain text rather than lose it.
                plain = html.unescape(re.sub(r"<[^>]+>", "", text))
                resp = await client.post(url, json={"chat_id": chat_id, "text": plain})
                why = _refusal(resp)
        if why:
            logger.warning("Telegram refused the %s message: %s", log_label, why)
            return False
        return True
    except Exception as e:
        logger.warning(
            "Failed to send %s Telegram message: %s",
            log_label, e, exc_info=True,
        )
        return False


def _refusal(resp: httpx.Response) -> str:
    """Telegram's reason for refusing a send, or "" when it took it."""
    try:
        data = resp.json()
    except ValueError:
        data = {}
    if data.get("ok") is True:
        return ""
    return str(data.get("description") or f"HTTP {resp.status_code}")[:200]


# ── Activity alerts: one layout for every site (spec 044) ────────────────────

_SINGULAR = {"replies": "reply", "kudos": "kudos", "watchers": "watcher"}


def activity_item(submission_id, title: str | None, **deltas: int) -> dict:
    """One post that gained activity this cycle: its id (for the canonical title), the site's title, and what
    went up, e.g. ``activity_item(sid, title, faves=2, comments=1)``. Zero and negative changes are dropped."""
    return {"id": str(submission_id), "title": title or "",
            "deltas": {k: int(v) for k, v in deltas.items() if v and int(v) > 0}}


def format_deltas(deltas: dict[str, int]) -> str:
    """``{"faves": 2, "comments": 1}`` → ``"+2 faves, +1 comment"``."""
    out = []
    for word, n in deltas.items():
        if n == 1:
            word = _SINGULAR.get(word, word[:-1] if word.endswith("s") else word)
        out.append(f"+{n:,} {word}")
    return ", ".join(out)


def canonical_title_map(platform: str, ids: list) -> dict[str, str]:
    """``{submission_id: linked piece's title}`` for the ids that are linked; {} on any failure (spec 044)."""
    try:
        from database import masterpiece_queries
        from database.db import get_connection
        conn = get_connection()
        try:
            return masterpiece_queries.canonical_titles(conn, platform, [str(i) for i in ids if i is not None])
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 — the site's own title is a fine fallback
        return {}


def _item_lines(platform: str, items: list[dict]) -> list[str]:
    """Plain (unescaped) ``"<title>: +2 faves"`` lines, titled by the linked piece where there is one."""
    titles = canonical_title_map(platform, [i["id"] for i in items])
    from polling.telegram import alert_title
    lines = []
    for i in items:
        title = alert_title(titles.get(i["id"]) or i["title"])
        what = format_deltas(i.get("deltas") or {})
        lines.append(f"{title}: {what}" if what else title)
    return lines


async def send_activity_alert(platform: str, items: list[dict], *, settings_key: str,
                              default_enabled: bool = True, account_id: int | None = None) -> None:
    """The toast and the Telegram alert for posts that gained activity, the same on every site (spec 044).

    ``🎨 DeviantArt · <account>`` then ``• <canonical title>: +2 faves, +1 comment`` per post. *settings_key* is the
    site's toast toggle (``da_notifications_enabled``), as before; Telegram follows ``telegram_enabled``.
    """
    if not items:
        return
    from polling.telegram import PLATFORM_NAME, alert_header
    lines = _item_lines(platform, items)
    import config
    settings = config.get_settings()
    maybe_show_toast(settings, settings_key, PLATFORM_NAME.get(platform, platform.upper()), lines,
                     default_enabled=default_enabled)
    target = _telegram_target(settings)
    if not target:
        return
    text = format_telegram_summary(alert_header(platform, account_id), [html.escape(x) for x in lines],
                                   TELEGRAM_DEFAULT_VISIBLE)
    await send_telegram(*target, text, log_label=platform.upper())


def _telegram_target(settings: dict[str, Any]):
    if not settings.get("telegram_enabled", False):
        return None
    from polling.telegram import notification_target
    return notification_target(settings)


# ── Convenience layer ─────────────────────────────────────────────────

def maybe_show_toast(
    settings: dict[str, Any],
    settings_key: str,
    title: str,
    lines: list[str],
    *,
    max_visible: int = TOAST_DEFAULT_VISIBLE,
    default_enabled: bool = True,
) -> bool:
    """Show a Windows toast iff the per-platform toggle is on and there's content.

    ``settings_key`` is the platform-specific "notifications enabled"
    flag (e.g. ``"sf_notifications_enabled"``, ``"notifications_enabled"``
    for Inkbunny). ``default_enabled`` is the value used when the key
    isn't present in settings — match each poller's historical default.
    """
    if not lines:
        return False
    if not settings.get(settings_key, default_enabled):
        return False
    return show_toast(title, lines, max_visible)


async def maybe_send_telegram_summary(
    settings: dict[str, Any],
    header_html: str,
    items: list[str],
    *,
    max_visible: int = TELEGRAM_DEFAULT_VISIBLE,
    log_label: str = "notification",
    platform: str | None = None,
) -> None:
    """Send a Telegram summary iff Telegram is enabled, configured, and items present.

    With *platform*, the message opens with the shared first line (``telegram.alert_header``: site, then the
    account) and *header_html* becomes the heading under it, e.g. ``<b>2 new kudos</b>`` (spec 044).

    The shared filter chain — ``telegram_enabled`` flag, bot token +
    chat id presence, non-empty items — runs here so callers stay
    focused on their platform-specific filtering (comments-only mode,
    fave-delta thresholds, etc) which they apply *before* calling in.
    """
    if not items:
        return
    if not settings.get("telegram_enabled", False):
        return
    from polling.telegram import notification_target as _nt
    _target = _nt(settings)     # private chat only (4.8.0)
    if not _target:
        return
    token, chat_id = _target
    if platform:
        from polling.telegram import alert_header
        header_html = alert_header(platform) + "\n" + header_html
    text = format_telegram_summary(header_html, items, max_visible)
    await send_telegram(token, chat_id, text, log_label=log_label)
