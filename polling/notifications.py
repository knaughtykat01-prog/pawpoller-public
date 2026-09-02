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
import logging
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
    """
    try:
        async with httpx.AsyncClient(timeout=TELEGRAM_HTTP_TIMEOUT) as client:
            await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            )
        return True
    except Exception as e:
        logger.warning(
            "Failed to send %s Telegram message: %s",
            log_label, e, exc_info=True,
        )
        return False


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
) -> None:
    """Send a Telegram summary iff Telegram is enabled, configured, and items present.

    The shared filter chain — ``telegram_enabled`` flag, bot token +
    chat id presence, non-empty items — runs here so callers stay
    focused on their platform-specific filtering (comments-only mode,
    fave-delta thresholds, etc) which they apply *before* calling in.
    """
    if not items:
        return
    if not settings.get("telegram_enabled", False):
        return
    token = settings.get("telegram_bot_token")
    chat_id = settings.get("telegram_chat_id")
    if not token or not chat_id:
        return
    # Prefix the header with the persona/account when the active poll cycle set a
    # multi-account context. No-op on single-account installs (prefix == "").
    platform, account_id = current_alert_account.get()
    if account_id is not None and header_html.startswith("<b>"):
        try:
            from polling.telegram import account_alert_prefix
            prefix = account_alert_prefix(platform, account_id)
            if prefix:
                header_html = "<b>" + prefix + header_html[3:]
        except Exception:
            pass
    text = format_telegram_summary(header_html, items, max_visible)
    await send_telegram(token, chat_id, text, log_label=log_label)
