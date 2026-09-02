"""Embedded browser login -- opens a pywebview popup for platform authentication.

The user logs in through the actual platform website inside a native desktop
window. On success, cookies/tokens are detected via URL changes and cookie
inspection, then extracted and saved to settings.json via config.save_settings().

pywebview's get_cookies() returns a list of http.cookies.SimpleCookie objects.
Each SimpleCookie is a dict mapping a single cookie name to a Morsel with a
.value attribute.  We flatten these into a {name: value} dict for easy lookup.

Threading constraints:
    pywebview only allows one ``webview.start()`` per process, and on Windows
    it must run on the main thread.  ``main.py`` already owns that call for
    the dashboard window.  This module therefore must NEVER call
    ``webview.start()`` itself — it only calls ``webview.create_window()``,
    which the existing GUI loop picks up and renders.  Cookie polling and
    the close handler run from worker threads; pywebview marshals the
    actual UI work back to the main thread internally.
"""

from __future__ import annotations

import logging
import queue
import threading
from http.cookies import SimpleCookie

import config

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Cookie helpers
# ---------------------------------------------------------------------------

def _flatten_cookies(cookie_list: list[SimpleCookie]) -> dict[str, str]:
    """Convert pywebview's list-of-SimpleCookie into a flat {name: value} dict.

    pywebview.Window.get_cookies() returns a list where each element is a
    SimpleCookie containing exactly one key.  We iterate all of them and
    merge into a single dict for easy lookup by cookie name.
    """
    flat: dict[str, str] = {}
    for sc in cookie_list:
        for name, morsel in sc.items():
            flat[name] = morsel.value
    return flat


# ---------------------------------------------------------------------------
# Per-platform login configuration
# ---------------------------------------------------------------------------
# Each entry defines:
#   name          -- Human-readable platform name (shown in window title)
#   url           -- Login page URL to load initially
#   success_check -- Callable(cookies_dict, current_url) -> bool
#                    Returns True when the user has successfully logged in.
#   extract       -- Callable(cookies_dict, current_url) -> dict
#                    Returns the credential keys to save into settings.json.
#   fields        -- Optional list of extra fields the user must provide
#                    before launching the browser (e.g. username to track).
#                    Each is {id, label, placeholder, required}.

PLATFORM_LOGIN: dict[str, dict] = {
    "ib": {
        "name": "Inkbunny",
        "url": "https://inkbunny.net/login.php",
        "success_check": lambda cookies, url: (
            "/login.php" not in (url or "")
            and "inkbunny.net" in (url or "")
        ),
        "extract": lambda cookies, url: {},
        # Inkbunny's API mints its own SID via api_login.php with
        # username + password — web session cookies aren't usable for
        # the API.  Browser login here is verification-only; the poller
        # and poster still authenticate via ib_username / ib_password.
        "fields": [],
    },
    "fa": {
        "name": "FurAffinity",
        "url": "https://www.furaffinity.net/login/",
        "success_check": lambda cookies, url: (
            "a" in cookies and "b" in cookies
        ),
        "extract": lambda cookies, url: {
            "fa_cookie_a": cookies.get("a", ""),
            "fa_cookie_b": cookies.get("b", ""),
        },
        "fields": [
            {"id": "fa_username", "label": "FA username", "placeholder": "Your FurAffinity username", "required": True},
        ],
    },
    "da": {
        "name": "DeviantArt",
        "url": "https://www.deviantart.com/users/login",
        "success_check": lambda cookies, url: "auth_secure" in cookies or "auth" in cookies,
        "extract": lambda cookies, url: {
            # DA uses full cookie string -- rebuild it from all cookies
            "da_cookie": "; ".join(f"{k}={v}" for k, v in cookies.items()),
        },
        "fields": [
            {"id": "da_username", "label": "DA username to track", "placeholder": "DeviantArt username", "required": True},
        ],
    },
    # SoFurry was removed here in 3.4.0. It authenticates with an official-API
    # Personal Access Token now, so there is no session cookie to capture — this
    # entry would have harvested cookies that nothing reads. Connect SF by pasting
    # a token (Settings → SoFurry), not by driving a browser login.
    "tw": {
        "name": "X / Twitter",
        "url": "https://x.com/i/flow/login",
        "success_check": lambda cookies, url: (
            "auth_token" in cookies
        ),
        "extract": lambda cookies, url: {
            "tw_auth_token": cookies.get("auth_token", ""),
            "tw_ct0": cookies.get("ct0", ""),
        },
        "fields": [
            {"id": "tw_username", "label": "X username to track", "placeholder": "Username (without @)", "required": True},
        ],
    },
    "ws": {
        "name": "Weasyl",
        "url": "https://www.weasyl.com/signin",
        "success_check": lambda cookies, url: "WZL" in cookies,
        "extract": lambda cookies, url: {},
        # Weasyl uses API keys, not cookies -- browser login captures nothing useful
        # but we include it so users can verify their account works.
        "fields": [],
    },
    "ao3": {
        "name": "Archive of Our Own",
        "url": "https://archiveofourown.org/users/login",
        "success_check": lambda cookies, url: (
            "user_credentials" in cookies or
            ("/users/login" not in (url or "") and "archiveofourown.org" in (url or ""))
        ),
        "extract": lambda cookies, url: {},
        # AO3 uses username/password stored in settings, not browser cookies
        "fields": [],
    },
    "sqw": {
        "name": "SquidgeWorld",
        "url": "https://squidgeworld.org/users/login",
        "success_check": lambda cookies, url: (
            "/users/login" not in (url or "") and
            "squidgeworld.org" in (url or "")
        ),
        "extract": lambda cookies, url: {},
        "fields": [],
    },
}


def get_supported_platforms() -> list[dict]:
    """Return list of platforms that support browser login.

    Each entry has {code, name, has_cookie_extraction, fields}.
    Used by the API to tell the frontend which platforms have browser login.
    """
    result = []
    for code, cfg in PLATFORM_LOGIN.items():
        # Only include platforms where browser login captures useful cookies
        extract_test = cfg["extract"]({}, "")
        result.append({
            "code": code,
            "name": cfg["name"],
            "has_cookie_extraction": bool(extract_test is not None),
            "fields": cfg.get("fields", []),
        })
    return result


# ---------------------------------------------------------------------------
# Core browser login function
# ---------------------------------------------------------------------------

def login_via_browser(
    platform: str,
    extra_fields: dict[str, str] | None = None,
    timeout: int = 300,
) -> dict | None:
    """Open a pywebview popup for the given platform's login page.

    The user logs in through the real platform website.  Once the
    success_check detects a successful login (via cookies or URL change),
    credentials are extracted and the window closes automatically.

    Args:
        platform: Platform code (e.g. "fa", "da", "tw").
        extra_fields: Additional field values from the UI (e.g. username).
        timeout: Max seconds to wait for login (default 300 = 5 minutes).

    Returns:
        Dict of extracted credentials on success, None on cancel/timeout.
        The credentials are also saved to settings.json automatically.

    Raises:
        ValueError: If platform code is not in PLATFORM_LOGIN.
        RuntimeError: If pywebview is not available (server mode).
    """
    plat_cfg = PLATFORM_LOGIN.get(platform)
    if not plat_cfg:
        raise ValueError(f"No browser login config for platform: {platform}")

    try:
        import webview
    except ImportError:
        raise RuntimeError(
            "pywebview is not available. Browser login only works in desktop mode."
        )

    # The dashboard's webview.start() (in main.py) must already be running.
    # If it isn't, we can't open a second window — and we mustn't try to
    # call webview.start() ourselves (Windows requires the main thread, and
    # only one start() per process is allowed).
    if not getattr(webview, "windows", None):
        raise RuntimeError(
            "Browser login requires the desktop GUI loop to be running. "
            "Open this from the desktop dashboard, not the headless server."
        )

    result_queue: queue.Queue[dict | None] = queue.Queue()
    poll_active = threading.Event()
    poll_active.set()

    try:
        window = webview.create_window(
            f"Login to {plat_cfg['name']} — PawPoller",
            plat_cfg["url"],
            width=900,
            height=700,
        )
    except Exception as e:
        logger.error("Failed to open browser login window for %s: %s", platform, e)
        return None

    def _on_closed():
        """User dismissed the window without completing login."""
        poll_active.clear()
        if result_queue.empty():
            result_queue.put(None)

    try:
        window.events.closed += _on_closed
    except Exception as e:
        # Older pywebview without the events API — fall back to polling-only.
        logger.debug("closed event unavailable for %s: %s", platform, e)

    def _check_login():
        """Poll the window's cookies/URL until success_check passes."""
        import time
        while poll_active.is_set():
            time.sleep(2)
            try:
                cookies_raw = window.get_cookies()
                url = window.get_current_url() or ""
                cookies = _flatten_cookies(cookies_raw)
                if plat_cfg["success_check"](cookies, url):
                    creds = plat_cfg["extract"](cookies, url)
                    logger.info(
                        "Browser login success for %s (%d cookie keys extracted)",
                        platform, len(creds),
                    )
                    result_queue.put(creds)
                    poll_active.clear()
                    try:
                        window.destroy()
                    except Exception:
                        pass
                    return
            except Exception as e:
                # Window might be closing or not yet loaded
                logger.debug("Cookie check for %s: %s", platform, e)

    threading.Thread(target=_check_login, daemon=True).start()

    # Wait for the result (blocks the FastAPI executor until the user
    # finishes login, closes the window, or the timeout fires).
    try:
        creds = result_queue.get(timeout=timeout)
    except queue.Empty:
        logger.warning("Browser login timed out for %s after %ds", platform, timeout)
        poll_active.clear()
        try:
            window.destroy()
        except Exception:
            pass
        return None

    if creds is None:
        logger.info("Browser login cancelled for %s", platform)
        return None

    # Merge extra fields (username, etc.) provided by the caller
    if extra_fields:
        creds.update(extra_fields)

    # Save to settings.json
    if creds:
        config.save_settings(creds)
        logger.info("Saved browser login credentials for %s: %s", platform, list(creds.keys()))

    return creds


# ---------------------------------------------------------------------------
# Convenience: check if browser login is available
# ---------------------------------------------------------------------------

def is_browser_login_available() -> bool:
    """Return True if pywebview is importable (desktop mode)."""
    try:
        import webview  # noqa: F401
        return True
    except ImportError:
        return False
