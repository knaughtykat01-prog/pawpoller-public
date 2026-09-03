"""Active session / cookie validity checks for the credential-bearing platforms.

The poll-derived health snapshot (``routes/api.py`` :: ``/platforms/health``) only
learns a session is bad *after* a poll fails. This module actively calls each
configured platform client's ``validate_session()`` so an expired cookie/token is
caught — and surfaced in the dashboard banner + the Settings status dots — before
it breaks a post or the next poll.

``validate_session()`` makes a real network request, and several platforms (AO3
most of all) rate-limit these, so the check runs on a *slow* cadence: once shortly
after startup, then roughly every 6 hours, plus an explicit user-triggered
"Check now". It deliberately never rides the 60 s health poll. Results live in a
process-local cache (the same pattern as the AO3 backoff cache) read by
``/api/platforms/sessions`` and folded into ``/api/platforms/health``.

Only the eight platforms whose client exposes ``validate_session()`` are
checkable; the rest (IB user/pass, FA/DA cookies, WS/WP/IK/TW tokens) have no
cheap standalone probe and fall back to poll-derived status.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import config

logger = logging.getLogger(__name__)

# code -> {"status": str, "detail": str | None, "checked_at": ISO str}
#   status: 'valid' | 'expired' | 'error' | 'unconfigured'
_session_health: dict[str, dict] = {}
_lock = asyncio.Lock()

# Platforms with a real validate_session() network check. Order = check order.
CHECKABLE: tuple[str, ...] = ("ao3", "sf", "sqw", "bsky", "mast", "tum", "pix",
                              "thr", "ig", "e621", "fn", "fbr", "tg")

# Human labels for log/UI fallback (the frontend has its own map too).
LABELS = {
    "ao3": "AO3", "sf": "SoFurry", "sqw": "SquidgeWorld", "bsky": "Bluesky",
    "mast": "Mastodon", "tum": "Tumblr", "pix": "Pixiv", "thr": "Threads",
    "ig": "Instagram", "e621": "e621", "fn": "FurryNetwork", "fbr": "Furbooru",
    "tg": "Telegram",
}

# What to tell the user when a check comes back CONFIRMED-failed. The default
# is about an expired credential, which is right for a cookie or a token and
# wrong for Telegram: a bot token does not expire, so the realistic cause is
# that the bot was removed from the channel or lost its admin rights. Sending
# someone to re-enter a token that was never the problem is worse than saying
# nothing.
_EXPIRED_DETAIL = {
    "tg": "Telegram refused the channel — check the bot is still an admin of it.",
}
_DEFAULT_EXPIRED_DETAIL = "Session/cookie is no longer valid — re-enter credentials."


def _configured(code: str, s: dict) -> bool:
    """Whether *code* has the credentials it needs to even attempt a check.
    Mirrors the gates in routes/api.py::_PLATFORM_HEALTH_CONFIG."""
    if code == "ao3":
        return bool((s.get("ao3_username") and s.get("ao3_password")) or s.get("ao3_session_cookie"))
    if code == "sf":
        return bool(s.get("sf_api_token"))
    if code == "sqw":
        return bool(s.get("sqw_username") and s.get("sqw_password"))
    if code == "bsky":
        return bool(s.get("bsky_identifier") and s.get("bsky_app_password"))
    if code == "mast":
        return bool(s.get("mast_instance_url") and s.get("mast_access_token"))
    if code == "tum":
        return bool(s.get("tum_api_key") and s.get("tum_blog"))
    if code == "pix":
        return bool(s.get("pix_refresh_token"))
    if code == "thr":
        return bool(s.get("thr_access_token"))
    if code == "ig":
        return bool(s.get("ig_access_token"))
    if code == "e621":
        return bool(s.get("e621_username") and s.get("e621_api_key"))
    if code == "fn":
        return bool(s.get("fn_username") and (s.get("fn_password") or s.get("fn_refresh_token")))
    if code == "fbr":
        return bool(s.get("fbr_username"))   # public read API — username is enough
    if code == "tg":
        # Either bot counts (reusing the notification bot is the documented
        # setup); a channel is mandatory because there is nothing to check
        # without one.
        return bool((s.get("tg_bot_token") or s.get("telegram_bot_token"))
                    and s.get("tg_channel"))
    return False


class _TgSessionProbe:
    """Adapter giving Telegram the ``validate_session()`` shape this module
    expects. Telegram has no session at all — a bot token is a permanent
    credential — so what is being checked is the RELATIONSHIP: is this bot
    still an admin of that channel?

    It deliberately calls ``getChatMemberCount`` rather than ``getChat``.
    ``getChat`` succeeds for any public channel including a stranger's, so it
    returns a confident yes for entirely the wrong chat (documentation_guide
    §55, observed live). ``getChatMemberCount`` is the call that fails with
    "bot is not a member of the channel chat" once the bot is removed — which
    is the failure this check exists to surface, and it is otherwise invisible
    until the next post fails.

    Reads flat settings, so it checks the DEFAULT channel only. Every other
    platform's probe has the same limitation; a per-account session check is a
    separate piece of work.
    """

    def __init__(self, settings: dict):
        self._settings = settings

    async def validate_session(self):
        from clients.tg.client import TgClient
        s = self._settings
        token = s.get("tg_bot_token") or s.get("telegram_bot_token", "")
        try:
            client = TgClient(bot_token=token, channel=s.get("tg_channel", ""))
        except ValueError as e:      # an invite link, not a channel
            raise RuntimeError(str(e)) from e
        # None on refusal, an int on success — never 0 for a failure, which
        # matters because a channel really can have very few members.
        return (await client.get_follower_count()) is not None


async def _validate(code: str, s: dict):
    """Build the platform's singleton client from settings and return its
    ``validate_session()`` result (truthy = alive). Each branch mirrors the
    corresponding ``/auth/*/connect`` route's client construction so the check
    warms the same session the pollers reuse."""
    if code == "ao3":
        from polling.ao3_poller import _get_or_create_client
        c = _get_or_create_client(
            s, s.get("ao3_username", ""), s.get("ao3_password", ""),
            s.get("ao3_target_user", ""), s.get("ao3_session_cookie", ""))
    elif code == "sf":
        from polling.sf_poller import _get_or_create_client
        c = _get_or_create_client(s, 0, True)
    elif code == "sqw":
        from polling.sqw_poller import _get_or_create_client
        c = _get_or_create_client(
            s, s.get("sqw_username", ""), s.get("sqw_password", ""),
            s.get("sqw_target_user", ""))
    elif code == "bsky":
        from polling.bsky_poller import _get_or_create_client
        c = _get_or_create_client(s, s.get("bsky_identifier", ""), s.get("bsky_app_password", ""))
    elif code == "mast":
        from polling.mast_poller import _get_or_create_client
        c = _get_or_create_client(s, s.get("mast_instance_url", ""), s.get("mast_access_token", ""))
    elif code == "tum":
        from polling.tum_poller import _get_or_create_client
        c = _get_or_create_client(s, s.get("tum_api_key", ""), s.get("tum_blog", ""))
    elif code == "pix":
        from polling.pix_poller import _get_or_create_client
        c = _get_or_create_client(s, s.get("pix_refresh_token", ""), s.get("pix_user_id", ""))
    elif code == "thr":
        from polling.thr_poller import _get_or_create_client
        c = _get_or_create_client(s, s.get("thr_access_token", ""), s.get("thr_user_id", ""))
    elif code == "ig":
        from polling.ig_poller import _get_or_create_client
        c = _get_or_create_client(s, s.get("ig_access_token", ""), s.get("ig_user_id", ""))
    elif code == "e621":
        from polling.e621_poller import _get_or_create_client
        c = _get_or_create_client(s, s.get("e621_username", ""), s.get("e621_api_key", ""))
    elif code == "fn":
        from polling.fn_poller import _get_or_create_client
        c = _get_or_create_client({
            "fn_username": s.get("fn_username", ""), "fn_password": s.get("fn_password", ""),
            "fn_refresh_token": s.get("fn_refresh_token", ""),
            "fn_access_token": s.get("fn_access_token", "")})
    elif code == "fbr":
        from polling.fbr_poller import _get_or_create_client
        c = _get_or_create_client(s, s.get("fbr_username", ""), s.get("fbr_api_key", ""))
    elif code == "tg":
        c = _TgSessionProbe(s)
    else:
        raise ValueError(f"unknown platform {code}")
    return await c.validate_session()


async def check_platform(code: str, s: dict | None = None) -> dict:
    """Validate one platform and update the cache. Returns its cache entry."""
    s = s if s is not None else config.get_settings()
    now = datetime.now(timezone.utc).isoformat()
    if not _configured(code, s):
        entry = {"status": "unconfigured", "detail": None, "checked_at": now}
        _session_health[code] = entry
        return entry
    try:
        result = await _validate(code, s)
        ok = bool(result)
        entry = {
            "status": "valid" if ok else "expired",
            "detail": None if ok else _EXPIRED_DETAIL.get(code, _DEFAULT_EXPIRED_DETAIL),
            "checked_at": now,
        }
    except Exception as e:
        # A network / transient failure is NOT proof of expiry. Mark 'error'
        # (amber) — distinct from a confirmed 'expired' (red) — so a blip
        # doesn't cry wolf and send the user chasing a perfectly good cookie.
        logger.warning("session check: %s failed: %s", code, e)
        entry = {"status": "error", "detail": str(e)[:200], "checked_at": now}
    _session_health[code] = entry
    # A user can mute a platform's session alert while they fix an external
    # problem (e.g. a Meta app-block). Auto-clear that mute the moment the
    # session validates again, so a *future* failure re-alerts — "mute until
    # fixed", never "mute forever". Re-read settings fresh (not the possibly
    # stale snapshot) so concurrent per-platform clears don't clobber.
    if entry["status"] == "valid" and code in (s.get("muted_session_codes") or []):
        fresh = config.get_settings().get("muted_session_codes") or []
        if code in fresh:
            config.save_settings({"muted_session_codes": [c for c in fresh if c != code]})
    return entry


async def check_all() -> dict:
    """Validate every checkable platform, serially (gentle on rate limits)."""
    s = config.get_settings()
    async with _lock:
        for code in CHECKABLE:
            await check_platform(code, s)
    logger.info("session check complete: %s",
                {k: v["status"] for k, v in _session_health.items()})
    return dict(_session_health)


def get_session_health() -> dict:
    """Process-local cached snapshot. Empty until the first check runs."""
    return dict(_session_health)


def summarize_problems(snapshot: dict | None = None) -> list[dict]:
    """Return the entries that need user attention (expired / error), each as
    ``{code, label, status, detail}`` — the shape the banner/notification layer
    consumes. 'unconfigured' and 'valid' are healthy and omitted."""
    snap = snapshot if snapshot is not None else _session_health
    out = []
    for code, entry in snap.items():
        if entry.get("status") in ("expired", "error"):
            out.append({
                "code": code,
                "label": LABELS.get(code, code.upper()),
                "status": entry["status"],
                "detail": entry.get("detail"),
            })
    return out
