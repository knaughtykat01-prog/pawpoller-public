"""Update BEFORE the window opens — the startup gate (4.9.0).

Until 4.9.0 the desktop opened its window first, the page asked GitHub for a
newer release, a banner appeared, and pressing it downloaded the build and
restarted the app — so every update was "get in, then get thrown out". This
module runs the same check and the same download/apply, just earlier: in
``main()`` before the database is opened or a thread is started. A small
native splash ("Updating PawPoller 4.8.0 → 4.9.0", progress bar) is the only
thing the user sees, and the next window they see is the new version.

Rules that matter more than the feature:

* **Fail open.** Offline, a slow GitHub, a bad download, a failed swap — every
  one of them logs one line and starts the version already installed. The
  in-app banner stays as the fallback path. An updater that can strand someone
  on a splash screen is worse than no updater.
* **Timeboxed.** The release check gets ``CHECK_TIMEOUT`` seconds. It runs in
  a thread and is simply abandoned if it has not answered; a cold start is
  never held hostage by a network call.
* **Only packaged builds.** ``updater.apply_update`` refuses unfrozen builds,
  and a developer updates with git pull — so the gate is a no-op from source.
* **Respect the person.** ``auto_update`` (default on) turns the gate off from
  Settings → About; ``update_skip_version`` lets one version be skipped, and
  the next one is offered again.

The splash is tkinter (stdlib; pinned in ``pawpoller.spec``). If it cannot be
shown — no display, a Linux build without Tk — the work runs without it and
the outcome is the same.
"""
from __future__ import annotations

import logging
import queue
import sys
import threading
from typing import Callable

import config

logger = logging.getLogger(__name__)

CHECK_TIMEOUT = 3.0          # seconds the release check may hold up a cold start


def _truthy(v, default=True) -> bool:
    if v is None:
        return default
    if isinstance(v, str):
        return v.strip().lower() not in ("", "0", "false", "no", "off")
    return bool(v)


def enabled(settings: dict, frozen: bool | None = None) -> tuple[bool, str]:
    """Whether the gate should run at all, and why not when it should not."""
    frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    if not frozen:
        return False, "not a packaged build"
    if not _truthy(settings.get("auto_update"), default=True):
        return False, "turned off in Settings"
    return True, ""


def check(timeout: float = CHECK_TIMEOUT, checker: Callable[[], dict] | None = None) -> dict | None:
    """Run the release check in a thread; None when it fails or is too slow.

    The thread is daemon and abandoned on timeout — it finishes on its own and
    nobody waits for it.
    """
    box: dict = {}
    fn = checker or _default_checker

    def run():
        try:
            box["info"] = fn()
        except Exception as e:          # network, GitHub, parsing — all "no update today"
            box["err"] = e

    t = threading.Thread(target=run, name="update-gate-check", daemon=True)
    t.start()
    t.join(timeout)
    if "info" not in box:
        logger.info("Startup update check: %s", "no answer within %.0fs" % timeout if "err" not in box else box["err"])
        return None
    return box["info"]


def _default_checker() -> dict:
    import updater
    return updater.check_for_update()


def decide(info: dict | None, settings: dict) -> str:
    """'apply' when a newer build exists and is not skipped; 'skip'; or 'none'."""
    if not info or not info.get("available") or not info.get("download_url"):
        return "none"
    latest = str(info.get("latest") or "")
    if latest and str(settings.get("update_skip_version") or "").strip() == latest:
        return "skip"
    return "apply"


# ── the splash ───────────────────────────────────────────────────────────────

def _with_splash(work: Callable[[Callable[[str, int], None]], None], title: str) -> None:
    """Run *work(report)* under a small native progress window when one can be
    shown, otherwise plainly. *report(text, percent)* updates the window. Any
    exception from *work* is re-raised after the window is gone."""
    try:
        import tkinter as tk
        from tkinter import ttk
        root = tk.Tk()
    except Exception as e:              # no Tk, no display — the work still happens
        logger.info("Update splash unavailable (%s); updating without it", e)
        work(lambda *_a: None)
        return

    root.title(title)
    root.resizable(False, False)
    root.attributes("-topmost", True)
    frame = ttk.Frame(root, padding=(22, 18))
    frame.grid()
    label = ttk.Label(frame, text="Checking…", font=("Segoe UI", 10))
    label.grid(row=0, column=0, sticky="w", pady=(0, 8))
    bar = ttk.Progressbar(frame, length=320, mode="determinate", maximum=100)
    bar.grid(row=1, column=0)
    root.update_idletasks()
    w, h = root.winfo_reqwidth(), root.winfo_reqheight()
    root.geometry(f"+{(root.winfo_screenwidth() - w) // 2}+{(root.winfo_screenheight() - h) // 2}")

    q: "queue.Queue[tuple]" = queue.Queue()
    outcome: dict = {}

    def report(text: str, pct: int) -> None:
        q.put(("progress", text, pct))

    def runner():
        try:
            work(report)
        except Exception as e:
            outcome["err"] = e
        q.put(("done",))

    threading.Thread(target=runner, name="update-gate-work", daemon=True).start()

    def poll():
        try:
            while True:
                item = q.get_nowait()
                if item[0] == "done":
                    root.after(150, root.destroy)
                    return
                label.configure(text=item[1])
                bar["value"] = item[2]
        except queue.Empty:
            pass
        root.after(100, poll)

    root.after(50, poll)
    root.mainloop()
    if "err" in outcome:
        raise outcome["err"]


# ── the gate ─────────────────────────────────────────────────────────────────

def run(settings: dict | None = None, *, checker=None, downloader=None, applier=None,
        exit_fn=sys.exit, timeout: float = CHECK_TIMEOUT) -> str:
    """Run the gate. Returns what happened, for the log and the tests:
    ``off:<reason>`` · ``timeout`` · ``none`` · ``skip`` · ``failed:<reason>`` ·
    ``applied`` (after which the process has been asked to exit so the swap
    script can relaunch the new build)."""
    s = settings if settings is not None else config.get_settings()
    ok, why = enabled(s)
    if not ok:
        return "off:" + why
    info = check(timeout=timeout, checker=checker)
    if info is None:
        return "timeout"
    verdict = decide(info, s)
    if verdict != "apply":
        return verdict

    current, latest = info.get("current", config.APP_VERSION), info.get("latest", "?")
    logger.info("Startup update: %s → %s", current, latest)

    def work(report):
        import updater
        report(f"Updating PawPoller {current} → {latest}", 8)
        path = (downloader or updater.download_update)(info["download_url"])
        report("Installing…", 78)
        (applier or updater.apply_update)(path)
        report("Restarting…", 100)

    try:
        _with_splash(work, title=f"PawPoller {latest}")
    except Exception as e:
        logger.warning("Startup update failed — starting the installed version instead: %s", e)
        return f"failed:{e}"
    logger.info("Startup update applied — relaunching as %s", latest)
    exit_fn(0)
    return "applied"
