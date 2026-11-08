"""Desktop window gates (074 STANDALONEBRIDGE).

Shared by the desktop window (``main.py``) and the local server's routes, which run in the same process but can't
import ``main``:

- ``own_page``: is the window showing the app's own dashboard (exact scheme, host and port)?
- ``ask``: the one way to raise a desktop yes/no; one question at a time, a kind refused for 10 s after a No.
- ``harden(webview)``: before any window, in both run modes. A guard in front of pywebview's ``js_bridge_call``
  (only names the window exposed, digits-only reply ids), and every browser permission refused in every window.
- ``remember_pick`` / ``take_pick``: add-from-file accepts only a file the desktop picker returned, once.
- ``check_folder_change``: in the desktop app, a new story/artwork/backup folder needs the person's desktop yes, and
  network locations are refused.

Design: specs/074-standalone-bridge/research.md R2-R8.
"""
from __future__ import annotations

import json
import logging
import ntpath
import posixpath
import re
import sys
import threading
import time
import unicodedata
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

_IS_WINDOWS = sys.platform == "win32"

MSG_BUSY = "Another question is already open on the desktop."
MSG_NOT_PICKED = "Pick the file again from your computer in the desktop app."
MSG_BAD_CODE = "That code doesn't look right. Copy it again from your server."
MSG_BAD_SERVER = "That server address doesn't look right. It should start with https://."
MSG_OFF_PAGE = "This only works from PawPoller's own pages."
MSG_FAILED = "Something went wrong. Try again."
MSG_FOLDER_REFUSED = "The folder wasn't changed."
MSG_FOLDER_NETWORK = ("Choose a folder on this computer's own drive. A network folder can't be used for this "
                      "in the desktop app.")
MSG_JOURNAL_OPEN = "FurAffinity's journal page is already open."
MSG_FOLDER_SEAT = ("On a hosted PawPoller your folders stay in its own storage. Leave this empty to use the "
                   "usual folder.")


# ── own page (R2) ─────────────────────────────────────────────────────────────

def folder_or_400(key: str, value):
    """The folder rule as a route uses it: the value to save, or a 400 with the refusal (074 FR-SEC-008)."""
    import config
    from fastapi import HTTPException
    err, val = check_folder_change(key, config.get_settings().get(key), value)
    if err:
        raise HTTPException(status_code=400, detail=err)
    return val


def own_page(current, start) -> bool:
    """The window shows the app's own dashboard: same scheme, host and port as it was opened on, no user part.
    Anything unknown, blank, ``about:``, ``blob:``, ``data:``, or any error → False."""
    try:
        if not isinstance(current, str) or not isinstance(start, str):
            return False
        c, s = urlsplit(current), urlsplit(start)
        if c.scheme not in ("http", "https") or c.username is not None or c.password is not None:
            return False
        return c.hostname is not None and (c.scheme, c.hostname, c.port) == (s.scheme, s.hostname, s.port)
    except Exception:
        return False


# ── one desktop question at a time (R8) ───────────────────────────────────────

NO_COOLDOWN = 10.0
_clock = time.monotonic
_confirm = None                       # set by the standalone window; None = no folder rule (R5)
_ask_lock = threading.Lock()
_last_no: dict[str, float] = {}


def set_confirm(fn) -> None:
    """The desktop's own yes/no dialog, ``fn(title, text) -> True | other``. Set only by the standalone window."""
    global _confirm
    _confirm = fn


def ask(kind: str, title: str, text: str, confirm=None) -> str:
    """``"yes"`` only when the person said yes in a desktop dialog. ``"busy"`` at once while another question is
    open (never queued), ``"no"`` for 10 s after any No, for a closed dialog, or if it fails."""
    fn = confirm or _confirm
    if fn is None:
        return "no"
    if not _ask_lock.acquire(blocking=False):
        return "busy"
    try:
        if _clock() - max(_last_no.values(), default=float("-inf")) < NO_COOLDOWN:   # any "no": a page can't rotate kinds
            return "no"
        try:
            yes = fn(title, text) is True
        except Exception as e:
            logger.warning("Desktop question failed: %s", type(e).__name__)
            yes = False
        if not yes:
            _last_no[kind] = _clock()
        return "yes" if yes else "no"
    finally:
        _ask_lock.release()


# ── picked files only (R3) ────────────────────────────────────────────────────

MAX_PICKS = 50
_picks: dict[str, bool] = {}          # insertion-ordered; the exact strings the picker returned
_picks_lock = threading.Lock()


def remember_pick(path) -> None:
    if not isinstance(path, str) or not path:
        return
    with _picks_lock:
        _picks.pop(path, None)
        _picks[path] = True
        while len(_picks) > MAX_PICKS:
            _picks.pop(next(iter(_picks)))


def take_pick(path) -> bool:
    """True once for a path the picker returned this run (exact string); it's used up."""
    if not isinstance(path, str):
        return False
    with _picks_lock:
        return _picks.pop(path, None) is not None


# ── folder settings (R5) ──────────────────────────────────────────────────────

FOLDER_LABELS = {"posting_story_archive_path": "story folder", "artwork_archive_path": "artwork folder",
                 "auto_backup_dir": "backup folder"}
_DRIVE_REMOVABLE, _DRIVE_FIXED = 2, 3


def _drive_type(root: str) -> int:
    import ctypes
    return int(ctypes.windll.kernel32.GetDriveTypeW(root))


def _odd_text(s: str) -> bool:
    return any(ch in "<>" or unicodedata.category(ch) in ("Cc", "Cf", "Zl", "Zp") for ch in s)


def check_folder_change(key: str, old, new) -> tuple[str | None, object]:
    """``(None, value)``: save exactly *value*. ``(message, None)``: refuse, save nothing from the request.
    Inactive (``(None, new)``) unless the standalone window set a confirm: a headless server is unchanged,
    except a hosted seat, whose folders stay inside its own storage (SEATFOLDERS)."""
    import config
    if not config.hosted_folder_ok(key, new.strip() if isinstance(new, str) else new):
        return MSG_FOLDER_SEAT, None
    if _confirm is None:
        return None, new
    if not isinstance(new, str) or len(new) > 260:
        return MSG_FOLDER_REFUSED, None
    v = new.strip()
    if not v:
        return None, ""                                  # back to the default: no folder of the page's choosing
    old_s = old.strip() if isinstance(old, str) else ""
    if v == old_s:
        return None, old                                 # not a change
    # The decision is pure string work: no stat, resolve or exists, so it never contacts a share.
    if _IS_WINDOWS:
        if not re.match(r"[A-Za-z]:[\\/]", v):           # raw text first: \foo, C:foo, UNC, \\?\, relative
            return MSG_FOLDER_NETWORK, None
        norm = ntpath.normpath(v)
        if not re.fullmatch(r"[A-Za-z]:\\.*", norm, re.S) or _drive_type(norm[:3]) not in (
                _DRIVE_REMOVABLE, _DRIVE_FIXED):
            return MSG_FOLDER_NETWORK, None
    else:
        if not v.startswith("/") or v.startswith("//"):
            return MSG_FOLDER_NETWORK, None
        norm = posixpath.normpath(v)
    if len(norm) > 200 or _odd_text(norm):
        return MSG_FOLDER_REFUSED, None
    if norm == old_s:
        return None, norm
    label = FOLDER_LABELS.get(key, "folder")
    text = (f"Use this folder for your {label}?\n\n{norm}\n\n"
            f"Only say yes if you just changed this in PawPoller yourself.")
    if key == "auto_backup_dir":
        text += "\n\nBackups include your saved passwords and sign-ins."
    answer = ask("folder:" + key, f"PawPoller: change your {label}?", text)
    if answer == "busy":
        return MSG_BUSY, None
    return (None, norm) if answer == "yes" else (MSG_FOLDER_REFUSED, None)


# ── harden: the transport guard (R7) and permissions (R4) ─────────────────────

_REPLY_ID = re.compile(r"\d{1,32}", re.ASCII)


def _make_guard(original):
    def guarded_js_bridge_call(window, func_name, param, value_id):
        """Only a name this window exposed, with a digits-only reply id, reaches pywebview: its reply is built as
        script from both. No internal names (PawPoller uses none). Never raises (on Qt this runs in a slot)."""
        try:
            if not (isinstance(func_name, str) and isinstance(value_id, str) and _REPLY_ID.fullmatch(value_id)):
                return None
            if func_name not in (getattr(window, "_functions", None) or {}):
                return None
            return original(window, func_name, param, value_id)
        except Exception as e:
            logger.warning("Desktop call dropped: %s", type(e).__name__)
            return None
    guarded_js_bridge_call._pp_guard = True
    return guarded_js_bridge_call


def mark_standalone(window, start_url: str) -> None:
    """The standalone main window: WebView2 also checks each message's own page against *start_url*."""
    window._pp_start_url = start_url


def mark_site_page(window) -> None:
    """A window showing another site's pages (FA journal, a sign-in): its alerts aren't shown (SEC4810 (1)).
    pywebview turns a page's alert() into a native Windows message box before any guard runs."""
    window._pp_site_page = True


def harden(webview) -> None:
    """Run once before any window, in both run modes. Idempotent."""
    if getattr(webview, "_pp_hardened", False):
        return
    util = webview.util
    guard = util.js_bridge_call if getattr(util.js_bridge_call, "_pp_guard", False) else _make_guard(
        util.js_bridge_call)
    util.js_bridge_call = guard                # backends imported later take it from here by name
    _set_backend_guards(guard)                 # ...and any already imported
    original_initialize = webview.initialize

    def initialize(*a, **k):
        # start() imports the backend here, before its first window is built: the classes can be patched now.
        gl = original_initialize(*a, **k)
        _set_backend_guards(guard)
        _patch_backends()
        return gl
    webview.initialize = initialize
    webview._pp_hardened = True


def _set_backend_guards(guard) -> None:
    for name, mod in list(sys.modules.items()):
        if name.startswith("webview.platforms.") and mod is not None and hasattr(mod, "js_bridge_call"):
            mod.js_bridge_call = guard  # pyright: ignore[reportAttributeAccessIssue]


def _patch_backends() -> None:
    for name, patch in (("webview.platforms.edgechromium", _patch_edge), ("webview.platforms.qt", _patch_qt)):
        mod = sys.modules.get(name)
        if mod is not None:
            try:
                patch(mod)
            except Exception as e:
                logger.error("Desktop window hardening (%s) failed: %s", name.rsplit(".", 1)[1], type(e).__name__)


def _log_fail(where: str, e: Exception) -> None:
    logger.warning("Desktop window %s: %s", where, type(e).__name__)


# WebView2 (Windows)

def _patch_edge(ec) -> None:
    cls = ec.EdgeChrome
    if getattr(cls, "_pp_patched", False):
        return
    orig_ready, orig_notify = cls.on_webview_ready, cls.on_script_notify

    def on_webview_ready(edge, sender, args):
        try:
            ready = bool(args.IsSuccess)
        except Exception:
            ready = False
        if not ready:
            return orig_ready(edge, sender, args)
        core = None
        try:
            core = sender.CoreWebView2
            core.PermissionRequested += _edge_deny        # before navigation (the original starts it)
            if hasattr(core, "ScreenCaptureStarting"):
                core.ScreenCaptureStarting += _edge_cancel_capture
            if hasattr(core, "LaunchingExternalUriScheme"):
                core.LaunchingExternalUriScheme += _edge_refuse_scheme
        except Exception as e:
            _log_fail("permission handler", e)
        ran = []

        def go():
            if not ran:
                ran.append(1)
                orig_ready(edge, sender, args)
        try:
            # Reset remembered Allows, then navigate. Chained on the UI thread, never waited on: WebView2
            # finishes its async calls on this same thread (research R4).
            task = core.Profile.GetNonDefaultPermissionSettingsAsync()

            def after(_t):
                try:
                    _edge_reset(core, task.Result)
                except Exception as e:
                    _log_fail("permission reset", e)
                go()
            task.ContinueWith(ec.Action[ec.Task](after), edge.syncContextTaskScheduler)
        except Exception as e:
            _log_fail("permission reset", e)
            go()

    def on_script_notify(edge, sender, args):
        start = getattr(getattr(edge, "pywebview_window", None), "_pp_start_url", None)
        if start is not None:
            try:
                ok = own_page(str(args.Source), start)
            except Exception:
                ok = False
            if not ok:
                return None                        # a message from another page in the standalone window
        elif getattr(getattr(edge, "pywebview_window", None), "_pp_site_page", False) and _is_alert(args):
            return None        # SEC4810 (1): a site's page (FA journal, sign-in) can't open a native message box
        return orig_notify(edge, sender, args)

    cls.on_webview_ready = on_webview_ready
    cls.on_script_notify = on_script_notify
    cls._pp_patched = True


def _is_alert(args) -> bool:
    """pywebview's alert message, which it shows as a native message box before any guard sees it.
    Anything unreadable → False (pywebview handles it as before)."""
    try:
        msg = json.loads(args.get_WebMessageAsJson())
        return isinstance(msg, list) and bool(msg) and msg[0] == "_pywebviewAlert"
    except Exception:
        return False


def _edge_deny(sender, args):
    try:
        from Microsoft.Web.WebView2.Core import CoreWebView2PermissionState  # pyright: ignore[reportMissingImports]  (pythonnet, Windows only)
        args.SavesInProfile = False
        args.State = CoreWebView2PermissionState.Deny
    except Exception as e:
        _log_fail("permission deny", e)


def _edge_cancel_capture(sender, args):
    try:
        args.Cancel = True
    except Exception as e:
        _log_fail("screen capture", e)


def _edge_refuse_scheme(sender, args):
    """SEC074 (a): the window navigated to a scheme Windows would hand to an app (ms-settings:, search-ms:, ...).
    Nothing launches; the app's own mail links open a new window, which goes through the guarded webbrowser.open."""
    try:
        args.Cancel = True
        logger.warning("Blocked a link that isn't a web address")
    except Exception as e:
        _log_fail("link scheme", e)


def _edge_reset(core, settings) -> None:
    from Microsoft.Web.WebView2.Core import CoreWebView2PermissionState  # pyright: ignore[reportMissingImports]  (pythonnet, Windows only)
    for s in list(settings or []):
        if s.PermissionState == CoreWebView2PermissionState.Allow:
            core.Profile.SetPermissionStateAsync(s.PermissionKind, s.PermissionOrigin,
                                                 CoreWebView2PermissionState.Default)


# Qt (Linux)

def _patch_qt(qt) -> None:
    bv = qt.BrowserView
    page_cls = bv.WebPage
    if getattr(page_cls, "_pp_patched", False):
        return
    if hasattr(page_cls, "onFeaturePermissionRequested"):
        def onFeaturePermissionRequested(page, url, feature):
            try:
                denied = qt.QWebPage.PermissionPolicy.PermissionDeniedByUser
            except AttributeError:
                denied = 2
            try:
                page.setFeaturePermission(url, feature, denied)
            except Exception as e:
                _log_fail("permission deny", e)
        page_cls.onFeaturePermissionRequested = onFeaturePermissionRequested
    orig_init = page_cls.__init__

    def __init__(page, parent=None, profile=None):
        if profile is not None:
            _qt_reset_profile(profile)
        orig_init(page, parent, profile=profile)
        _qt_connect_denials(page)
        _qt_refuse_schemes(qt, page)
    page_cls.__init__ = __init__
    page_cls._pp_patched = True
    # Qt's bridge slot parses the page's JSON before the guard sees it; PyQt aborts the app on an exception that
    # escapes a slot unless sys.excepthook is set (research R7, tasks review). Log instead.
    if sys.excepthook is sys.__excepthook__:
        sys.excepthook = _log_unhandled


def _log_unhandled(kind, value, tb) -> None:
    """Where it broke (module:line of the innermost frame), never the message: it can carry a path (SEC074 c)."""
    where = ""
    try:
        while tb is not None and tb.tb_next is not None:
            tb = tb.tb_next
        if tb is not None:
            where = f" at {tb.tb_frame.f_globals.get('__name__', '?')}:{tb.tb_lineno}"
    except Exception:
        pass
    logger.error("Unhandled error in the desktop window: %s%s", getattr(kind, "__name__", "error"), where)


def _qt_refuse_schemes(qt, page) -> None:
    """SEC074 (a): Qt passes an unknown scheme to the desktop when a click navigates to it; refuse them all. New-window
    links (the app's mail links) go through pywebview's NavigationHandler to the guarded webbrowser.open instead."""
    try:
        policy = qt.QWebEngineSettings.UnknownUrlSchemePolicy.DisallowUnknownUrlSchemes
        page.settings().setUnknownUrlSchemePolicy(policy)
    except Exception as e:
        _log_fail("link schemes", e)


def _qt_reset_profile(profile) -> None:
    try:
        if hasattr(profile, "setPersistentPermissionsPolicy"):
            profile.setPersistentPermissionsPolicy(type(profile).PersistentPermissionsPolicy.AskEveryTime)
        if hasattr(profile, "listAllPermissions"):
            for p in profile.listAllPermissions():
                p.reset()
    except Exception as e:
        _log_fail("permission reset", e)


def _qt_safe(fn):
    def run(*a):
        try:
            fn(*a)
        except Exception as e:
            _log_fail("permission", e)
    return run


def _qt_connect_denials(page) -> None:
    try:
        if hasattr(page, "desktopMediaRequested"):
            page.desktopMediaRequested.connect(_qt_safe(lambda req: req.cancel()))
        if hasattr(page, "permissionRequested"):
            page.permissionRequested.connect(_qt_safe(lambda perm: perm.deny()))
    except Exception as e:
        _log_fail("permission signals", e)
