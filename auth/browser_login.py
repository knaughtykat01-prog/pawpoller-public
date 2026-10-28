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
import re
import threading
from http.cookies import SimpleCookie

import config

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Cookie helpers
# ---------------------------------------------------------------------------

def _domain_matches(domain: str, base: str) -> bool:
    """True when a cookie/host *domain* is *base* or a subdomain of it, at a dot boundary
    (so ``evilfuraffinity.net`` is not ``furaffinity.net``)."""
    d = (domain or "").strip().lower().lstrip(".")
    return bool(d) and (d == base or d.endswith("." + base))


def _host_is(url: str, base: str) -> bool:
    """The page's host is *base* or a subdomain of it (a parsed host, never a substring)."""
    from urllib.parse import urlsplit
    try:
        return _domain_matches(urlsplit(url or "").hostname or "", base)
    except ValueError:
        return False


def _flatten_cookies(cookie_list: list[SimpleCookie], base: str | None = None) -> dict[str, str]:
    """Convert pywebview's list-of-SimpleCookie into a flat {name: value} dict.

    pywebview.Window.get_cookies() returns a list where each element is a
    SimpleCookie containing exactly one key.  We iterate all of them and
    merge into a single dict for easy lookup by cookie name.

    With *base* (spec 059, H-new-1) only that site's own cookies are kept, and a
    cookie with no domain is dropped. On Linux the Qt backend returns EVERY cookie
    in the shared webview store, including the dashboard's ``pp_session`` and other
    sites' sign-ins; DeviantArt and Newgrounds join all of them into one string, so
    without this a hand-over carried them all. It runs before ``success_check``, so
    a foreign ``a``/``b`` or ``auth_token`` can't confirm a sign-in either.
    """
    flat: dict[str, str] = {}
    for sc in cookie_list:
        for name, morsel in sc.items():
            if base is not None and not _domain_matches(morsel["domain"], base):
                continue
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
#   domain        -- The site's registrable domain. Only its cookies are read,
#                    and the success checks test the page's parsed host (059).
#   extract_js    -- Optional (059): {key, expr, valid}. Once success_check
#                    passes, ``expr`` runs in the page (window.evaluate_js); the
#                    result is kept under ``key`` only if ``valid`` returns it.
#                    For a token held in page storage rather than a cookie.

PLATFORM_LOGIN: dict[str, dict] = {
    "ib": {
        "name": "Inkbunny",
        "url": "https://inkbunny.net/login.php",
        "domain": "inkbunny.net",
        "success_check": lambda cookies, url: (
            "/login.php" not in (url or "") and _host_is(url, "inkbunny.net")
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
        "domain": "furaffinity.net",
        "success_check": lambda cookies, url: (
            "a" in cookies and "b" in cookies and _host_is(url, "furaffinity.net")
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
        "domain": "deviantart.com",
        "success_check": lambda cookies, url: (
            ("auth_secure" in cookies or "auth" in cookies) and _host_is(url, "deviantart.com")
        ),
        "extract": lambda cookies, url: {
            # DA uses full cookie string -- rebuild it from all cookies
            "da_cookie": "; ".join(f"{k}={v}" for k, v in cookies.items()),
        },
        "fields": [
            {"id": "da_target_user", "label": "DA username to track", "placeholder": "DeviantArt username", "required": True},
        ],
    },
    # SoFurry was removed here in 3.4.0. It authenticates with an official-API
    # Personal Access Token now, so there is no session cookie to capture — this
    # entry would have harvested cookies that nothing reads. Connect SF by pasting
    # a token (Settings → SoFurry), not by driving a browser login.
    "tw": {
        "name": "X / Twitter",
        "url": "https://x.com/i/flow/login",
        "domain": "x.com",
        "success_check": lambda cookies, url: (
            "auth_token" in cookies and _host_is(url, "x.com")
        ),
        "extract": lambda cookies, url: {
            "tw_auth_token": cookies.get("auth_token", ""),
            "tw_ct0": cookies.get("ct0", ""),
        },
        "fields": [
            # ⚠ The id IS the settings key this is saved under, so it must be the
            # CANONICAL credential field. It was `tw_username`, which nothing in
            # the codebase reads — the poller, the auth-status endpoint and the
            # poster all want `tw_target_user` — so a successful browser login
            # saved working cookies and an unreachable username, and every poll
            # then failed validation against an EMPTY screen name while blaming
            # the cookies (4.3.3).
            {"id": "tw_target_user", "label": "X username to track", "placeholder": "Username (without @)", "required": True},
        ],
    },
    "ws": {
        "name": "Weasyl",
        "url": "https://www.weasyl.com/signin",
        "domain": "weasyl.com",
        "success_check": lambda cookies, url: "WZL" in cookies and _host_is(url, "weasyl.com"),
        "extract": lambda cookies, url: {},
        # Weasyl uses API keys, not cookies -- browser login captures nothing useful
        # but we include it so users can verify their account works.
        "fields": [],
    },
    "ao3": {
        "name": "Archive of Our Own",
        "url": "https://archiveofourown.org/users/login",
        "domain": "archiveofourown.org",
        "success_check": lambda cookies, url: _host_is(url, "archiveofourown.org") and (
            "user_credentials" in cookies or "/users/login" not in (url or "")
        ),
        "extract": lambda cookies, url: {},
        # AO3 uses username/password stored in settings, not browser cookies
        "fields": [],
    },
    "sqw": {
        "name": "SquidgeWorld",
        "url": "https://squidgeworld.org/users/login",
        "domain": "squidgeworld.org",
        "success_check": lambda cookies, url: (
            "/users/login" not in (url or "") and _host_is(url, "squidgeworld.org")
        ),
        "extract": lambda cookies, url: {},
        "fields": [],
    },
    # Newgrounds (4.23.0, MEDIAPLATS §5): no API, so the browser session is the credential.
    # The full cookie string is kept (the DA pattern) — the site's session cookie name is
    # opaque and can change; the client sends them all. Success = we left the login page
    # on newgrounds.com with at least one cookie set. ❓ Whose session it is gets checked
    # by the client (`activeuser` + "name" on any signed-in page) before the first post.
    "ng": {
        "name": "Newgrounds",
        "url": "https://www.newgrounds.com/passport",
        "domain": "newgrounds.com",
        "success_check": lambda cookies, url: (
            bool(cookies) and _host_is(url, "newgrounds.com")
            and "/passport" not in (url or "") and "/login" not in (url or "")
        ),
        "extract": lambda cookies, url: {
            "ng_cookie": "; ".join(f"{k}={v}" for k, v in cookies.items()),
        },
        "fields": [
            # The id IS the settings key (the 4.3.3 lesson): the canonical field.
            {"id": "ng_username", "label": "Newgrounds username", "placeholder": "Your Newgrounds username", "required": True},
        ],
    },
    # Itaku (spec 059): the API token sits in the page's Local Storage under ``token`` (the
    # Itaku guide's screenshot), not in a cookie, so ``extract_js`` reads it once the page has
    # left the sign-in screen. Anything that isn't a plain token string is refused, and the
    # value is never logged.
    "ik": {
        "name": "Itaku",
        "url": "https://itaku.ee/auth/login",   # /login is Itaku's "Page not found" (4.88.2)
        "domain": "itaku.ee",
        "success_check": lambda cookies, url: (
            _host_is(url, "itaku.ee") and "/login" not in (url or "") and "/sign-up" not in (url or "")
        ),
        "extract": lambda cookies, url: {},
        "extract_js": {"key": "ik_auth_token", "expr": "window.localStorage.getItem('token')",
                       "valid": lambda v: _clean_token(v)},
        "fields": [
            {"id": "ik_target_user", "label": "Itaku username", "placeholder": "Your Itaku username", "required": True},
        ],
    },
}

_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.\-]{16,256}$")


def _clean_token(value) -> str:
    """A page-storage token, or "" when it isn't one: a string of token characters, at
    most 256 long, with any JSON quotes taken off. Never log the value."""
    if not isinstance(value, str):
        return ""
    v = value.strip()
    if len(v) >= 2 and v[0] == v[-1] == '"':
        v = v[1:-1]
    return v if _TOKEN_RE.match(v) else ""


def get_supported_platforms() -> list[dict]:
    """Return list of platforms that support browser login.

    Each entry has {code, name, has_cookie_extraction, fields}.
    Used by the API to tell the frontend which platforms have browser login.
    """
    result = []
    for code, cfg in PLATFORM_LOGIN.items():
        # Only include platforms where browser login captures useful cookies
        result.append({
            "code": code,
            "name": cfg["name"],
            # 059: true only where the window keeps something (it was always true: `{} is not None`).
            "has_cookie_extraction": bool(saved_keys(code)),
            "fields": cfg.get("fields", []),
        })
    return result


# ---------------------------------------------------------------------------
# What a browser sign-in may save (spec 059, C1 / FR-SEC-001)
# ---------------------------------------------------------------------------

MAX_VALUE_LEN = 8 * 1024


def saved_keys(platform: str) -> set[str]:
    """The secrets the window itself captures for *platform* (cookies, page-storage token).
    Empty for the check-only sites (Inkbunny, Weasyl, AO3, SquidgeWorld)."""
    cfg = PLATFORM_LOGIN.get(platform) or {}
    if not cfg:
        return set()
    keys = set(cfg["extract"]({}, "").keys())
    if cfg.get("extract_js"):
        keys.add(cfg["extract_js"]["key"])
    return keys & set(config.PLATFORM_CREDENTIAL_FIELDS.get(platform, []))


def identity_keys(platform: str) -> set[str]:
    """The account-name fields the person types (``fa_username``, ``tw_target_user``…)."""
    cfg = PLATFORM_LOGIN.get(platform) or {}
    return {f["id"] for f in cfg.get("fields", [])} & set(config.PLATFORM_CREDENTIAL_FIELDS.get(platform, []))


def check_creds(platform: str, creds: dict) -> None:
    """Raise ValueError unless every key is one of *platform*'s own sign-in fields and every
    value is a string of at most 8 KB. Without this a hand-over could write any setting —
    ``auth_password_hash`` included, as a bare key for a default account."""
    allowed = saved_keys(platform) | identity_keys(platform)
    for k, v in creds.items():
        if k not in allowed or not isinstance(v, str) or len(v) > MAX_VALUE_LEN:
            raise ValueError("That isn't a sign-in for this site.")


# ---------------------------------------------------------------------------
# Core browser login function
# ---------------------------------------------------------------------------

def _save_browser_creds(platform: str, creds: dict, account_id: int | None) -> dict:
    """Write the captured credentials under the RIGHT account's keys (4.6.3).

    This was ``config.save_settings(creds)`` — the bare canonical keys, i.e.
    the platform's DEFAULT account, whichever account the user was logging in
    for (BACKLOG BLOGINACCT). With an ``account_id`` the keys are namespaced
    through ``config.account_setting_key``, exactly as the Accounts page saves
    a pasted credential; without one (the platform-level button) the default
    slot is still the target, as before. Returns the keys written.
    """
    check_creds(platform, creds)   # every caller: the local window, a connected desktop, the helper (059)
    payload = dict(creds)
    if account_id is not None:
        from database.db import get_connection
        from database import accounts as _accts
        conn = get_connection()
        try:
            acct = _accts.get_account(conn, int(account_id))
        finally:
            conn.close()
        if not acct or acct.get("platform") != platform:
            raise ValueError(f"Account {account_id} is not a {platform} account")
        payload = {config.account_setting_key(int(account_id), k, bool(acct.get("is_default"))): v
                   for k, v in creds.items()}
    config.save_settings(payload)
    return payload


def login_via_browser(
    platform: str,
    extra_fields: dict[str, str] | None = None,
    timeout: int = 300,
    account_id: int | None = None,
    save: bool = True,
) -> dict | None:
    """Open a pywebview popup for the given platform's login page.

    The user logs in through the real platform website.  Once the
    success_check detects a successful login (via cookies or URL change),
    credentials are extracted and the window closes automatically.

    Args:
        platform: Platform code (e.g. "fa", "da", "tw").
        extra_fields: Additional field values from the UI (e.g. username).
        timeout: Max seconds to wait for login (default 300 = 5 minutes).
        account_id: which account row the result belongs to (None = default slot).
        save: False returns the credentials without saving them here; a connected
            desktop or the sign-in helper hands them to a server instead (059).

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
                cookies = _flatten_cookies(cookies_raw, plat_cfg.get("domain"))
                if plat_cfg["success_check"](cookies, url):
                    creds = plat_cfg["extract"](cookies, url)
                    js = plat_cfg.get("extract_js")
                    if js:
                        value = js["valid"](window.evaluate_js(js["expr"]))
                        if not value:
                            continue        # not signed in yet (or not a token): keep waiting
                        creds[js["key"]] = value
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

    # Save to settings.json — under the chosen account's keys (4.6.3).
    if creds and save:
        written = _save_browser_creds(platform, creds, account_id)
        logger.info("Saved browser login credentials for %s (account %s): %s",
                    platform, account_id if account_id is not None else "default", list(written.keys()))

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
