"""One seat at a time per site, across every seat on the home box (4.72.0, spec 037).

Seats share the box's one home address. Each seat paces its own requests, but three seats checking FurAffinity at
the same moment still look like three times the traffic from one address. ``homebox.sh`` mounts one folder into every
seat (``PAWPOLLER_TURN_DIR``); before a site's poll cycle runs it takes that site's turn, an exclusive ``flock`` on
``<dir>/<code>.lock``.

Why ``flock``: the kernel releases it the moment the holding process dies, so a crashed or stopped seat never leaves
a turn stuck, and every seat shares the box's one kernel through the bind mount. No stale-lock stealing is needed.

Every seat can write the shared folder, so it is treated as hostile: a lock file is never followed through a link,
must be a plain file, and nothing is ever written into it (security review 4.72.0).

Installs without the variable (desktop, the VM, self-hosters) never wait: the decorator passes straight through.
"""
from __future__ import annotations

import asyncio
import functools
import logging
import os
import re
import stat
import time
from pathlib import Path

try:
    import fcntl
except ImportError:   # Windows: never on the box, so never needed
    fcntl = None

logger = logging.getLogger(__name__)

RETRY_SECONDS = 3.0
_warned = False
_held: set[str] = set()   # sites this process holds the turn for (flock is per open file, so it'd wait on itself)


class TurnTimeout(Exception):
    def __init__(self, code: str):
        self.code = code
        # 4.73.0 (spec 038): every caller (scheduled and manual) gets here, so the skip is recorded once.
        from polling.last_check import record_skip
        record_skip(code, "waited 10 minutes for its turn")
        super().__init__(f"Skipped: another PawPoller on this server was checking {_site_name(code)} for too long. "
                         "It runs next round.")


class SitePaused(TurnTimeout):
    """The box operator paused this site on every seat (spec 042). A TurnTimeout, so every caller skips it alike."""
    def __init__(self, code: str):
        self.code = code
        from polling.last_check import record_skip
        record_skip(code, "paused by the server operator", count=False)
        Exception.__init__(self, f"Skipped: the server operator has paused {_site_name(code)} on every PawPoller "
                                 "on this server.")


_CODE_RE = re.compile(r"[a-z0-9]{1,16}")


def operator_paused() -> set[str]:
    """Sites the box operator paused (``homebox.sh pause``), read fresh every call; empty off the box.

    The folder is mounted read-only into every seat, so no seat can change it (unlike the turn folder). Still read
    defensively: never through a link, at most 4 KB, only code-shaped lines.
    """
    raw = (os.environ.get("PAWPOLLER_OPERATOR_DIR") or "").strip()
    if not raw:
        return set()
    try:
        fd = os.open(Path(raw) / "paused", os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except OSError:
        return set()
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return set()
        text = os.read(fd, 4096).decode("ascii", "ignore")
    finally:
        os.close(fd)
    return {s for s in (line.strip() for line in text.splitlines()) if _CODE_RE.fullmatch(s)}


def _site_name(code: str) -> str:
    try:
        from database.accounts import PLATFORM_NAMES
        return PLATFORM_NAMES.get(code, code)
    except Exception:  # noqa: BLE001 — a name is cosmetic
        return code


def _wait_limit() -> float:
    try:
        return max(float(os.environ.get("PAWPOLLER_TURN_WAIT_SECONDS") or 600), 0.0)
    except ValueError:
        return 600.0


def enabled() -> Path | None:
    """The shared turn folder, or None (no turn-taking here)."""
    global _warned
    raw = (os.environ.get("PAWPOLLER_TURN_DIR") or "").strip()
    if not raw:
        return None
    d = Path(raw)
    if fcntl is None or not d.is_dir() or not os.access(d, os.W_OK):
        if not _warned:
            logger.warning("Site turns are off: %s isn't a folder this PawPoller can write to.", raw)
            _warned = True
        return None
    return d


def _set_waiting(code: str) -> None:
    try:
        from polling.multi_account import get_poll_progress
        p = get_poll_progress().get(code)
        if p is not None:
            p.update(active=True, phase="waiting", current=0, total=0,
                     message="Waiting for its turn: another PawPoller on this server is checking it")
    except Exception:  # noqa: BLE001 — progress is cosmetic
        pass


def _clear_waiting(code: str) -> None:
    try:
        from polling.multi_account import get_poll_progress
        p = get_poll_progress().get(code)
        if p is not None and p.get("phase") == "waiting":
            p.update(active=False, phase="idle", message="")
    except Exception:  # noqa: BLE001
        pass


_OPEN_FLAGS = (os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
               | getattr(os, "O_NOCTTY", 0))


def _try_open(path: Path) -> int | None:
    """A plain single-link file's descriptor, or None for anything else (a link, a folder, a pipe, a device)."""
    try:
        fd = os.open(path, _OPEN_FLAGS, 0o600)
    except OSError:
        return None
    st = os.fstat(fd)
    if stat.S_ISREG(st.st_mode) and st.st_nlink == 1:
        return fd
    os.close(fd)
    return None


def _open_lock(code: str, folder: Path) -> int:
    """The lock file's descriptor: never through a link, a plain file only.

    Anything else in its place (a planted link, folder or pipe) is removed once and the file made afresh, so one
    seat can't block a site for every seat by squatting on its name (release review 4.72.0). ``unlink`` never
    follows a link; a non-empty folder can't be removed and that site skips the round.
    """
    path = folder / f"{code}.lock"
    fd = _try_open(path)
    if fd is None:
        logger.warning("The %s turn file wasn't a plain file; replacing it.", code)
        try:
            if path.is_dir() and not path.is_symlink():
                path.rmdir()
            else:
                path.unlink()
        except OSError:
            pass
        fd = _try_open(path)
    if fd is None:
        logger.warning("The %s turn file can't be made safely; skipping this round.", code)
        raise TurnTimeout(code)
    return fd


async def _take(code: str, folder: Path) -> int:
    """Lock ``<folder>/<code>.lock``; returns the held descriptor (closing it releases the turn)."""
    fd = _open_lock(code, folder)
    deadline = time.monotonic() + _wait_limit()
    waited = False
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TurnTimeout(code) from None
                if not waited:
                    waited = True
                    _set_waiting(code)
                    logger.info("Waiting for the %s turn (another PawPoller on this server has it).", code)
                await asyncio.sleep(RETRY_SECONDS)
    except BaseException:
        os.close(fd)
        if waited:
            _clear_waiting(code)
        raise
    if waited:
        _clear_waiting(code)   # some pollers never overwrite their progress (Telegram, an early return)
    return fd


def site_turn(code: str):
    """Decorate ``run_<code>_poll_cycle`` so each call takes the site's turn first (pass-through off the box)."""
    def deco(fn):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            if code in operator_paused():
                raise SitePaused(code)
            folder = enabled()
            if folder is None or code in _held:
                # Off the box, or this seat is already checking the site: the poller's own lock decides
                # (it skips a second run, as it always has).
                return await fn(*args, **kwargs)
            fd = await _take(code, folder)
            _held.add(code)
            try:
                return await fn(*args, **kwargs)
            finally:
                _held.discard(code)
                os.close(fd)
        wrapper.__site_turn__ = code
        return wrapper
    return deco
