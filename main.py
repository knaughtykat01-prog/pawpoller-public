"""Unified entry point — server + poller + native desktop window.

Architecture overview
---------------------
PawPoller runs as a single process with a handful of daemon threads plus the
main thread:

  Uvicorn web server    -- serves the FastAPI dashboard
  One poller per platform -- started by polling.desktop_pollers.start_all(),
                             which is driven by the poll-cycle REGISTRY. Until
                             4.3.2 this file hand-wrote sixteen near-identical
                             poller functions and the list stopped at `ig`, so
                             e621, FurryNetwork, Furbooru and Telegram polled
                             on the server and never here.
  Telegram digest       -- periodic cross-platform stats digest
  Telegram bot listener -- responds to /poll and friends
  Posting scheduler     -- fires queued/scheduled posts
  pystray tray icon     -- system tray menu/icon

  Main thread:          -- pywebview window, the native desktop GUI wrapper

All background threads are **daemon threads** so they are killed automatically
when the main thread (pywebview) exits.  This avoids zombie processes and
means we do not need explicit shutdown signalling for the pollers or server.

Each poller thread creates its own asyncio event loop because asyncio loops
are not thread-safe.  A dedicated loop per thread lets each poller use
async/await for non-blocking HTTP calls without interfering with the others.

Usage:
    python main.py          # dev mode
    PawPoller.exe   # frozen build
"""

import logging
import socket
import sys
import threading
import time
from datetime import datetime

import uvicorn

import config
from database.db import init_db


# ── Logging ───────────────────────────────────────────────────
# Dual-output logging: stdout for dev console visibility, plus a persistent
# log file under the APPDATA (frozen) or project (dev) logs directory.

from logging.handlers import RotatingFileHandler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        RotatingFileHandler(
            str(config.LOGS_DIR / "app.log"),
            maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8",
        ),
    ],
)

# Strip credentials from every record before any handler sees it (2.193.1) —
# httpx logs full request URLs, several platforms put tokens in them. Desktop
# app.log is as exposed as the server's: users attach it to bug reports.
import log_redaction
log_redaction.install()
config.refresh_log_secrets()   # seed the secret list; config pushes on every save

logger = logging.getLogger("main")


# ── 6-Hourly Telegram Digest ─────────────────────────────────
# Sends a cross-platform stats digest every 6 hours via Telegram.
# Uses its own asyncio event loop like the pollers.

def _start_digest_scheduler():
    """Run periodic Telegram digest in its own daemon thread."""
    import asyncio
    from datetime import datetime, timezone
    from polling.telegram import send_digest_report

    def _get_digest_interval() -> int:
        """Read digest interval from settings (in seconds)."""
        hours = config.get_settings().get("telegram_digest_interval_hours", 6)
        return max(int(hours), 1) * 60 * 60

    def _seconds_until_next_digest() -> float:
        """Calculate seconds until next digest is due, respecting last sent time."""
        digest_interval = _get_digest_interval()
        MIN_STARTUP_DELAY = 300         # 5 minutes minimum after startup
        last = config.get_settings().get("last_digest_sent_at")
        if last:
            try:
                last_dt = datetime.fromisoformat(last)
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=timezone.utc)
                elapsed = (datetime.now(timezone.utc) - last_dt).total_seconds()
                remaining = digest_interval - elapsed
                if remaining > MIN_STARTUP_DELAY:
                    return remaining
                return MIN_STARTUP_DELAY
            except (ValueError, TypeError):
                pass
        return MIN_STARTUP_DELAY  # First ever digest — wait 5 min for pollers

    async def _run():
        initial_delay = _seconds_until_next_digest()
        logger.info("Telegram digest scheduler started (next digest in %.0f min)", initial_delay / 60)
        await asyncio.sleep(initial_delay)
        while True:
            try:
                await send_digest_report()
            except Exception as e:
                logger.error("Digest report failed: %s", e)
            await asyncio.sleep(_get_digest_interval())

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_run())
    except Exception as e:
        logger.debug("Digest scheduler thread exiting: %s", e)  # Daemon teardown


# ── Telegram Bot Command Listener ─────────────────────────────
# Long-polls Telegram for incoming commands and dispatches them.

def _start_telegram_bot():
    """Run Telegram bot command listener in its own daemon thread."""
    import asyncio
    from polling.telegram_bot import run_bot

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(run_bot())
    except Exception as e:
        logger.debug("Telegram bot thread exiting: %s", e)  # Daemon teardown


# ── Background web server (uvicorn) ──────────────────────────
# The FastAPI dashboard is served by uvicorn in a daemon thread.
# pywebview (the native window) points its embedded browser at this
# local server, so the entire UI is just a web app rendered natively.
# Running as a daemon thread means it dies automatically when main exits.

def _start_server():
    """Run uvicorn in a daemon thread."""
    logger.info("Uvicorn thread starting...")
    try:
        # Import here (not at top-level) to avoid circular imports --
        # dashboard module may import config, and config is still
        # being initialised when top-level imports run.
        from dashboard import app as dash_app
        uvicorn.run(
            dash_app,
            host=config.DASHBOARD_HOST,
            port=config.DASHBOARD_PORT,
            log_level="info",
        )
    except Exception as e:
        logger.error("Uvicorn failed to start: %s", e, exc_info=True)


# ── System tray (pystray) ────────────────────────────────────
# The tray icon provides a "minimize to tray" experience: when the user
# closes the window with tray mode enabled, the window hides instead of
# destroying, and the tray icon becomes visible so they can restore it.
#
# Lifecycle:
#   1. Tray icon is CREATED and its thread is STARTED during main(), but
#      it begins with visible=False (via a no-op setup callback) so the
#      icon does not appear in the system tray until the user minimises.
#   2. When the user closes the window and minimize_to_tray is on,
#      _on_closing() hides the window and sets tray visible=True.
#   3. Clicking "Show" in the tray menu restores the window and hides
#      the tray icon again.
#   4. Clicking "Quit" in the tray menu destroys both the window and the
#      tray icon, which unblocks webview.start() and lets main() exit.

_tray_icon = None   # pystray.Icon instance, set in main()
_window = None      # pywebview window instance, set in main()


def _load_tray_image():
    """Load the tray icon image via Pillow."""
    from PIL import Image
    icon_path = config.resource_path("assets/tray_icon.png")
    try:
        return Image.open(str(icon_path))
    except Exception:
        # Fallback: procedurally generate a simple bar-chart icon if the
        # asset file is missing (e.g. during early dev or broken build)
        from PIL import ImageDraw
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.ellipse([2, 2, 62, 62], fill=(34, 37, 47, 255), outline=(108, 140, 255, 255), width=2)
        draw.rectangle([14, 38, 22, 50], fill=(108, 140, 255, 255))
        draw.rectangle([26, 28, 34, 50], fill=(108, 140, 255, 255))
        draw.rectangle([38, 18, 46, 50], fill=(108, 140, 255, 255))
        return img


def _show_window(icon=None, item=None):
    """Restore the pywebview window from tray.

    Called when the user clicks "Show" in the tray context menu (or
    double-clicks the tray icon, since Show is marked as default=True).
    """
    global _window
    if _window is not None:
        _window.show()           # Make the hidden pywebview window visible again
    if _tray_icon is not None:
        _tray_icon.visible = False  # Hide the tray icon until next minimize


def _quit_app(icon=None, item=None):
    """Full exit -- destroy window and stop tray icon.

    Stopping the tray icon ends its thread, and destroying the window
    unblocks webview.start() in main(), allowing the process to exit.
    """
    global _tray_icon, _window
    logger.info("Quit requested from tray — shutting down.")
    if _tray_icon is not None:
        _tray_icon.stop()
        _tray_icon = None
    if _window is not None:
        _window.destroy()  # Unblocks webview.start() in main()


def _create_tray_icon():
    """Create the pystray system tray icon (not yet started).

    The icon is created here but NOT run -- main() starts it in a separate
    daemon thread with a no-op setup callback to keep it initially hidden.
    """
    import pystray
    from pystray import MenuItem

    image = _load_tray_image()
    menu = pystray.Menu(
        MenuItem("Show", _show_window, default=True),  # default=True: double-click action
        MenuItem("Quit", _quit_app),
    )
    icon = pystray.Icon("PawPoller", image, "PawPoller", menu)
    return icon


def _minimize_to_tray_enabled() -> bool:
    """Check whether 'minimize to tray' is enabled in settings.

    When disabled (default), closing the window exits the app normally.
    When enabled, closing hides to tray instead.
    """
    settings = config.get_settings()
    return settings.get("minimize_to_tray", False)


def _on_closing():
    """pywebview closing callback -- intercepts the window close event.

    pywebview calls this before destroying the window.  The return value
    controls behaviour:
      - return False: CANCEL the close, keeping the window alive (hidden).
        Used when minimize_to_tray is on -- we hide the window and show
        the tray icon instead of exiting.
      - return True: ALLOW the close, which destroys the window and
        unblocks webview.start(), letting main() proceed to shutdown.
    """
    global _tray_icon, _window
    if _minimize_to_tray_enabled():
        logger.info("Minimising to system tray instead of closing.")
        if _window is not None:
            _window.hide()        # Hide window but keep process running
        if _tray_icon is not None:
            _tray_icon.visible = True  # Show tray icon so user can restore
        return False  # Cancel the close -- window stays alive but hidden
    return True  # Allow normal close -- app will exit


# ── Main ──────────────────────────────────────────────────────
# Startup sequence:
#   1. Initialise the SQLite database (create tables if first run)
#   2. Launch 4 daemon threads: web server + 3 platform pollers
#   3. Launch the system tray icon in a 5th daemon thread (hidden)
#   4. Block until the uvicorn server is accepting TCP connections
#   5. Open the pywebview native window pointing at the local server
#   6. webview.start() blocks the main thread until the window is destroyed
#   7. On exit, clean up the tray icon and let daemon threads die

def _sync_settings_on_startup():
    """Pull settings from the paired server at startup, if syncing is on.

    Gated on auto_sync_enabled (default true) — the same switch as the
    background auto-sync loops. Used to be gated on credential_mode, but
    the vault went always-on in 2.101.0 and storage mode was never really
    the right proxy for "do I want my paired server's settings".
    """
    import httpx

    settings = config.get_settings()
    if not settings.get("auto_sync_enabled", True):
        logger.info("Settings sync: auto-sync disabled, skipping startup pull")
        return

    server_url = settings.get("posting_server_url", "").rstrip("/")
    api_key = settings.get("posting_server_api_key", "")
    if not server_url or not api_key:
        logger.debug("Settings sync: no server URL or API key configured, skipping")
        return

    try:
        resp = httpx.post(
            f"{server_url}/api/settings/sync",
            json={"mode": "pull"},
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10,
        )
        if resp.status_code != 200:
            logger.warning("Settings sync pull failed: HTTP %d", resp.status_code)
            return
        data = resp.json()
        if data.get("ok") and data.get("settings"):
            pulled = data["settings"]
            config.merge_synced_settings(pulled)
            logger.info("Settings sync: pulled %d keys from server", len(pulled))
        else:
            logger.warning("Settings sync: server returned ok=false")
    except Exception as e:
        logger.warning("Settings sync: pull failed (server unreachable?): %s", e)


def main():
    """Entry: the update gate first, then whichever mode this install is in."""
    try:
        import update_gate
        _gate = update_gate.run()
        logger.info("Startup update gate: %s", _gate)
        if str(_gate).startswith("failed:"):
            import techcentre
            techcentre.report("update", "update_gate.run", "UpdateFailed", str(_gate))
    except Exception as e:
        logger.warning("Startup update gate skipped: %s", e)
    from desktop_agent import connect_target, decide_mode
    settings = config.get_settings()
    if decide_mode(sys.argv[1:], settings) == "connected":
        url, key = connect_target(sys.argv[1:], settings)
        if url and key:
            return run_connected(url, key)
        logger.warning("connected mode without a server URL/key — falling back to standalone")
    return run_standalone()


def run_connected(server_url: str, api_key: str):
    """A window onto the server + the local agent (SYNCTRUTH, 4.13.0).

    No init_db, no pollers, no scheduler, no Telegram bot, no auto-sync, no mirror
    watcher: everything lives on the server. This process keeps only the update gate
    (already run), the tray, the agent's write-behind queue, and the pywebview window.
    """
    global _tray_icon, _window
    import webview
    import desktop_agent

    # A desktop that just migrated from paired mode still has the old copy on disk (4.14.0).
    desktop_agent.retire_local_database(config.DATA_DIR)
    agent = desktop_agent.Agent(server_url, api_key)
    agent.start()
    logger.info("Connected mode: window onto %s (agent queue: %d pending)", server_url, agent.queue.pending_count())

    _tray_icon = _create_tray_icon()
    threading.Thread(target=_tray_icon.run, kwargs={"setup": lambda icon: None}, daemon=True).start()

    reachable = agent.server_reachable()
    kwargs = dict(width=1200, height=800, min_size=(800, 500), js_api=desktop_agent.AgentApi(agent))
    if reachable:
        _window = webview.create_window("PawPoller", url=server_url, **kwargs)
    else:
        _window = webview.create_window("PawPoller", html=desktop_agent.offline_page(server_url, agent), **kwargs)
        agent.when_reachable(lambda: _window.load_url(server_url))
    _window.events.closing += _on_closing
    _start_kwargs = {}
    if sys.platform.startswith("linux"):
        _start_kwargs["gui"] = "qt"
    webview.start(**_start_kwargs)
    if _tray_icon is not None:
        _tray_icon.stop()
    agent.stop()
    logger.info("Window closed — shutting down (connected).")


def run_standalone():
    global _tray_icon, _window

    # --- Step 0: Update before anything loads (4.9.0) --- (now in main(); kept as a no-op marker)
    # Packaged builds only, timeboxed, fails open. When a newer release exists
    # this downloads it under a small splash, applies it and exits so the swap
    # script relaunches the new build — the window the user sees is the new
    # version, never "get in, then restart". SystemExit passes straight through
    # the except below on purpose.
    # --- Step 1: Database initialisation ---
    logger.info("Initialising database...")
    init_db()  # Creates tables/schema if the DB file does not exist yet

    # --- Step 1b: Sync settings from server (cloud mode) ---
    _sync_settings_on_startup()

    # --- Step 1c: Start the recurring background pull thread so settings
    # changed on another device flow back into this desktop install
    # without requiring a restart.
    try:
        import auto_sync
        auto_sync.start_pull_thread()
        logger.info("Auto-sync pull thread started (every %ds)",
                    auto_sync.AUTO_SYNC_PULL_INTERVAL_SECONDS)
    except Exception as e:
        logger.warning("Auto-sync pull thread failed to start: %s", e)

    # --- Step 2: Launch daemon threads ---
    # All threads are daemon=True so they terminate automatically when
    # the main thread (pywebview) exits.  No explicit shutdown is needed.

    logger.info("Starting web server on http://%s:%d", config.DASHBOARD_HOST, config.DASHBOARD_PORT)
    server_thread = threading.Thread(target=_start_server, daemon=True)
    server_thread.start()

    # --- Polling ownership gate ---
    # If this desktop install is paired with a remote server, the server
    # owns the poll loop. Starting our own pollers would duplicate every
    # request and double-fire "all polls complete" notifications.
    polling_owner = config.get_polling_owner("desktop")
    setup_mode = config.get_settings().get("setup_mode") or "(inferred)"
    if polling_owner == "local":
        # One thread per platform in the poll-cycle registry (4.3.2). The
        # sixteen hand-written starts this replaced stopped at `ig`; the count
        # in this log line was hard-coded at 11 and had been wrong for longer.
        from polling.desktop_pollers import start_all
        started = start_all()
        logger.info("Polling owner: local desktop (mode=%s) — started %d poller threads: %s",
                    setup_mode, len(started), ", ".join(started))

        logger.info("Starting Telegram digest scheduler...")
        digest_thread = threading.Thread(target=_start_digest_scheduler, daemon=True)
        digest_thread.start()
    else:
        logger.info("Polling owner: remote server (mode=%s) — local pollers + digest skipped",
                    setup_mode)

    # The Telegram bot, posting scheduler, and uvicorn server run regardless
    # of polling ownership. The bot listens for /poll commands the user might
    # send manually, and posting is a desktop-side action even when paired.
    logger.info("Starting Telegram bot command listener...")
    bot_thread = threading.Thread(target=_start_telegram_bot, daemon=True)
    bot_thread.start()

    logger.info("Starting posting scheduler...")
    from posting.scheduler import start_posting_scheduler
    posting_thread = threading.Thread(target=start_posting_scheduler, daemon=True, name="Posting scheduler")
    posting_thread.start()

    # Auto-backup (gap G7) — runs regardless of polling ownership; the local
    # instance's own data (DB + settings + media) is worth protecting even when
    # paired. Self-throttles on the enabled flag + last_auto_backup_at.
    logger.info("Starting auto-backup scheduler...")
    from routes.backup_api import run_auto_backup_scheduler
    autobackup_thread = threading.Thread(target=run_auto_backup_scheduler, daemon=True, name="Auto-backup")
    autobackup_thread.start()

    # --- Step 3: System tray icon (initially hidden) ---
    _tray_icon = _create_tray_icon()
    # pystray's default setup callback sets visible=True, which would show
    # the tray icon immediately.  We pass a no-op lambda to override that
    # behaviour so the icon starts HIDDEN and only appears when the user
    # triggers minimize-to-tray via _on_closing().
    tray_thread = threading.Thread(
        target=_tray_icon.run,
        kwargs={"setup": lambda icon: None},  # No-op: keep icon hidden on start
        daemon=True,
    )
    tray_thread.start()
    logger.info("System tray icon ready.")

    # --- Step 4: Wait for the server to accept connections ---
    # The uvicorn server runs in a daemon thread and takes a moment to bind
    # the port.  We poll with TCP connect attempts (socket handshake only,
    # no HTTP request) until the port is open, with a 15-second timeout.
    # This prevents pywebview from opening a window to a server that is
    # not yet ready, which would show a blank or error page.
    url = f"http://{config.DASHBOARD_HOST}:{config.DASHBOARD_PORT}"
    logger.info("Waiting for server at %s:%d ...", config.DASHBOARD_HOST, config.DASHBOARD_PORT)
    deadline = time.time() + 15  # Absolute deadline -- 15 seconds from now
    attempts = 0
    while time.time() < deadline:
        attempts += 1
        try:
            # A successful TCP connection means uvicorn is listening
            with socket.create_connection((config.DASHBOARD_HOST, config.DASHBOARD_PORT), timeout=1.0):
                logger.info("Server ready after %d attempts (%.1fs)", attempts, time.time() - (deadline - 15))
                break
        except OSError as e:
            if attempts % 10 == 0:  # Log every ~2 seconds (10 * 0.2s) to avoid spam
                logger.info("Still waiting for server... attempt %d (%s)", attempts, e)
            time.sleep(0.2)  # 200ms between connection attempts
    else:
        # for/else: this block runs if the loop exhausted without break
        logger.error("SERVER DID NOT START within 15s after %d attempts!", attempts)
        logger.error("Server thread alive: %s", server_thread.is_alive())
        sys.exit(1)

    # --- Step 5: Open the native desktop window ---
    # pywebview creates a native OS window with an embedded browser that
    # loads the local dashboard URL.  This gives PawPoller the look and
    # feel of a native desktop app while the UI is actually a web app.
    import webview

    class _DesktopApi:
        """JS bridge exposed to the dashboard as window.pywebview.api.*.

        Each method is callable from the frontend and returns a Promise on the
        JS side. The Artwork hub uses open_image_dialog so the desktop app can
        pick a local image by path (copied into the archive server-side) instead
        of round-tripping the bytes through a browser upload.
        """
        def open_image_dialog(self):
            try:
                win = webview.windows[0] if webview.windows else None
                if win is None:
                    return []
                result = win.create_file_dialog(
                    webview.OPEN_DIALOG,
                    allow_multiple=False,
                    file_types=(
                        "Image files (*.png;*.jpg;*.jpeg;*.gif;*.webp)",
                        "All files (*.*)",
                    ),
                )
                return list(result) if result else []
            except Exception as e:
                logger.error("open_image_dialog failed: %s", e)
                return []

    logger.info("Opening native window at %s", url)
    _window = webview.create_window(
        "PawPoller",
        url=url,
        width=1200,
        height=800,
        min_size=(800, 500),
        js_api=_DesktopApi(),
    )

    # Register the closing callback so we can intercept the close event
    # and redirect to tray instead of exiting (when that setting is on).
    # pywebview uses += to add event handlers (observer pattern).
    _window.events.closing += _on_closing

    # --- Step 6: Block until the window is destroyed ---
    # webview.start() runs the native event loop on the main thread.
    # It blocks here until the window is DESTROYED (not just hidden).
    # When minimize-to-tray is active, _on_closing returns False to
    # prevent destruction, so this only unblocks on a true exit.
    #
    # On Linux force the Qt backend explicitly. pywebview's default
    # GTK backend needs PyGObject + WebKit2GTK system bindings that
    # are brittle to bundle via PyInstaller (and AppImage); Qt with
    # QtWebEngine is pip-installable, ships its own native libs, and
    # bundles cleanly. Windows and macOS use their native backend.
    _start_kwargs = {}
    if sys.platform.startswith("linux"):
        _start_kwargs["gui"] = "qt"
    webview.start(**_start_kwargs)

    # --- Step 7: Cleanup ---
    # Stop the tray icon thread if it is still running (e.g. the user
    # closed the window normally without going through tray "Quit").
    if _tray_icon is not None:
        _tray_icon.stop()

    # All daemon threads die automatically now that the main thread is exiting.
    logger.info("Window closed — shutting down.")


if __name__ == "__main__":
    main()
