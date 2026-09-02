"""Background settings sync between desktop and cloud server.

Two halves to this module:

  - **Push side**: when the desktop saves settings, we fire-and-forget a
    debounced push to the cloud server so the change propagates within
    a couple of seconds. Debouncing collapses bursts (e.g. five
    save_settings() calls in 100ms during a wizard step) into one HTTP
    request. Runs on a daemon thread.

  - **Pull side**: a separate daemon thread polls the cloud server every
    AUTO_SYNC_PULL_INTERVAL seconds and merges anything new. Conflict
    resolution is last-writer-wins via mtime — if the server's snapshot
    is older than ours we skip the merge (otherwise we'd undo our own
    in-flight push).

Loop-protection: merge_synced_settings() ultimately calls save_settings()
which would normally trigger a push, which the server would then echo
back to us, ad infinitum. The `_in_pull_merge` thread-local flag is set
during pull-driven saves and skipped by schedule_push().

Activation: nothing happens unless settings.json has both
`posting_server_url` and `posting_server_api_key` set AND
`auto_sync_enabled` is true (the default). Servers themselves leave
posting_server_url empty so they never sync to themselves.
"""
from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger(__name__)


AUTO_SYNC_PUSH_DEBOUNCE_SECONDS = 2.0
AUTO_SYNC_PULL_INTERVAL_SECONDS = 300  # 5 minutes
AUTO_SYNC_HTTP_TIMEOUT = 10
# Cap exponential backoff at 1 hour. After this many consecutive failures
# we plateau instead of growing further — avoids drifting into "next
# attempt in 12 hours" territory but stops pestering an unreachable
# server every 5 minutes forever.
AUTO_SYNC_PULL_MAX_BACKOFF_SECONDS = 3600

_push_lock = threading.Lock()
_push_timer: threading.Timer | None = None

# Set on the thread that's currently applying a server pull, so save_settings
# inside merge_synced_settings doesn't trigger another push.
_in_pull_merge = threading.local()


def _is_pull_merge() -> bool:
    return getattr(_in_pull_merge, "active", False)


def _sync_target():
    """Return (server_url, api_key) if auto-sync is configured, else None.

    Security: rejects non-HTTPS URLs for non-localhost targets so the
    bearer token never crosses the network in plaintext. Localhost
    keeps http:// because the loopback never leaves the machine.
    """
    import config
    settings = config.get_settings()
    if not settings.get("auto_sync_enabled", True):
        return None
    # The headless server never pushes anywhere — it is the source of
    # truth in any pairing. Without this guard a server with a stray
    # `posting_server_url` value (e.g. accidentally set during setup,
    # or pointing at itself) tries to push to that target every time
    # settings change, which is wasted work at best and a config loop
    # at worst.
    if settings.get("setup_mode") == config.SETUP_MODE_SERVER:
        return None
    server_url = (settings.get("posting_server_url") or "").rstrip("/")
    api_key = settings.get("posting_server_api_key") or ""
    if not server_url or not api_key:
        return None
    # A server pointing at itself would create a loopback storm. Cheap guard:
    # treat anything resolving to localhost as "we are the server, don't sync".
    is_loopback = "localhost" in server_url or "127.0.0.1" in server_url
    if is_loopback:
        return None
    # Refuse to push the bearer token over plain HTTP. The cost of a
    # misconfigured http:// target is the API key (and full settings dump
    # including platform credentials) on the wire in cleartext — much
    # worse than just not syncing.
    if not server_url.lower().startswith("https://"):
        logger.warning(
            "Auto-sync disabled: posting_server_url must use https:// "
            "for non-localhost targets (got %r)", server_url,
        )
        return None
    return server_url, api_key


def _do_push() -> int:
    """Push current settings to the server. Runs on the debounce timer thread.

    Returns the number of keys the server merged, or -1 if it was not paired /
    unreachable / errored. (The debounce-timer caller ignores the return; the
    manual push_now() button surfaces it.)"""
    import httpx
    import config

    target = _sync_target()
    if target is None:
        return -1
    server_url, api_key = target

    try:
        data, _mtime = config.get_settings_for_sync()
        resp = httpx.post(
            f"{server_url}/api/settings/sync",
            json={"mode": "push", "settings": data},
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=AUTO_SYNC_HTTP_TIMEOUT,
        )
        if resp.status_code == 200:
            body = resp.json()
            merged = int(body.get("keys_merged", 0))
            logger.info("Auto-sync push: %d keys merged on server", merged)
            return merged
        logger.warning("Auto-sync push failed: HTTP %d", resp.status_code)
        return -1
    except Exception as e:
        logger.debug("Auto-sync push: server unreachable (%s)", e)
        return -1


def schedule_push() -> None:
    """Fire a debounced push to the server. Safe to call from any thread.

    Multiple calls within AUTO_SYNC_PUSH_DEBOUNCE_SECONDS collapse into one
    HTTP request, so wizards/bulk saves don't generate a push per field.
    No-op when the save originated from a pull merge.
    """
    if _is_pull_merge():
        return
    if _sync_target() is None:
        return

    global _push_timer
    with _push_lock:
        if _push_timer is not None:
            _push_timer.cancel()
        _push_timer = threading.Timer(AUTO_SYNC_PUSH_DEBOUNCE_SECONDS, _do_push)
        _push_timer.daemon = True
        _push_timer.start()


def _pull_attempt(force: bool = False) -> tuple[bool, bool]:
    """Pull from the cloud server.

    Returns ``(reachable, applied)``:
      - ``reachable`` is True when the request completed at the HTTP
        layer (any 200, including "I have nothing newer for you"). False
        only on transport errors or non-200 responses.
      - ``applied`` is True when fresh settings were merged into the
        local copy.

    The loop in ``_pull_loop`` only backs off when ``reachable`` is
    False, so the common case of "server reachable but nothing new"
    doesn't get treated as a failure.
    """
    import httpx
    import config

    target = _sync_target()
    if target is None:
        # Not configured — treat as reachable so we don't back off
        # waiting for a sync that's been turned off.
        return True, False
    server_url, api_key = target

    try:
        resp = httpx.post(
            f"{server_url}/api/settings/sync",
            json={"mode": "pull"},
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=AUTO_SYNC_HTTP_TIMEOUT,
        )
    except Exception as e:
        logger.debug("Auto-sync pull: server unreachable (%s)", e)
        return False, False

    if resp.status_code != 200:
        logger.warning("Auto-sync pull failed: HTTP %d", resp.status_code)
        return False, False

    body = resp.json()
    if not body.get("ok") or not body.get("settings"):
        return True, False
    pulled = body["settings"]
    server_mtime = body.get("timestamp", 0)
    local_mtime = (config.SETTINGS_PATH.stat().st_mtime
                   if config.SETTINGS_PATH.exists() else 0)
    # Last-writer-wins: only apply if the server is newer than us.
    # Skipping when server is older avoids stomping a push we just sent
    # that the server hasn't fully echoed back yet. A *manual* pull (force)
    # bypasses this — the user explicitly clicked "Pull from server", so
    # apply the server's copy even if the local file was touched more recently.
    if not force and server_mtime <= local_mtime:
        return True, False
    _in_pull_merge.active = True
    try:
        config.merge_synced_settings(pulled, client_timestamp=server_mtime)
    finally:
        _in_pull_merge.active = False
    logger.info("Auto-sync pull: applied %d keys from server", len(pulled))
    return True, True


def pull_once(force: bool = False) -> bool:
    """One-shot pull. Returns True iff fresh settings were applied.

    Kept for the backwards-compat surface (main.py / tests). Internally
    delegates to ``_pull_attempt`` which exposes the richer state the
    pull loop needs to make backoff decisions. ``force`` bypasses the
    last-writer-wins mtime guard (the manual "Pull from server" button).
    """
    _reachable, applied = _pull_attempt(force=force)
    return applied


def push_now() -> int:
    """Synchronous one-shot push to the server (the manual "Push to server"
    button). Bypasses the debounce timer. Returns the number of keys the
    server merged, or -1 if it was unreachable / not paired."""
    return _do_push()


def _pull_loop():
    """Background loop: pull from server, with backoff on transport failure.

    Steady state sleeps AUTO_SYNC_PULL_INTERVAL_SECONDS (5 min) between
    cycles. Only true *transport* failures (connection refused, timeout,
    non-200 response) increase the sleep — getting back a 200 with "no
    new settings for you" is the common case and resets the counter.

    Schedule: 5m → 10m → 20m → 40m → 60m cap. So an unreachable server
    isn't pestered every 5 minutes forever, but a server that's just
    quiet stays on the regular cadence and picks up changes promptly.
    """
    base = AUTO_SYNC_PULL_INTERVAL_SECONDS
    cap = AUTO_SYNC_PULL_MAX_BACKOFF_SECONDS
    consecutive_failures = 0
    while True:
        try:
            reachable, _applied = _pull_attempt()
        except Exception:
            logger.exception("Auto-sync pull loop iteration failed")
            reachable = False

        if reachable:
            consecutive_failures = 0
            sleep_for = base
        else:
            consecutive_failures += 1
            sleep_for = min(base * (2 ** (consecutive_failures - 1)), cap)
            logger.debug(
                "Auto-sync pull: backing off %ds after %d consecutive failures",
                sleep_for, consecutive_failures,
            )
        time.sleep(sleep_for)


def start_pull_thread() -> threading.Thread:
    """Start the periodic pull daemon. Idempotent — call once at startup."""
    t = threading.Thread(target=_pull_loop, name="auto_sync_pull", daemon=True)
    t.start()
    return t
