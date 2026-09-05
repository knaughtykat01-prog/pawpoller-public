"""Self-update for the installed, dockerless PawPoller Server (HOSTFREE §3, 4.12.0).

The installers (``installer/server/``) lay the server out like this and run it from a
service unit:

    <root>/releases/<version>/PawPoller-Server[.exe] + _internal/…
    <root>/current  -> releases/<version>      (symlink on POSIX, junction on Windows)

The unit's ExecStart points at ``<root>/current/…``. Updating is therefore: download the
matching release asset, verify its ``.sha256``, unpack it *beside* the running build, flip
``current``, and exit with ``RESTART_EXIT_CODE``. The service manager (systemd
``Restart=always``, Task Scheduler restart-on-failure, launchd ``KeepAlive``) starts the new
build; the old one stays on disk for a rollback (``prune`` keeps the previous one).

Nothing here runs unless the installer's environment says the process is managed
(``PAWPOLLER_SERVER_MANAGED=1`` + ``PAWPOLLER_SERVER_ROOT``). A Docker container, a source
checkout or the desktop app never see those, so they are untouched. The ``auto_update``
preference (the desktop gate's switch, 4.9.0) is honoured here too.
"""
from __future__ import annotations

import hashlib
import logging
import os
import platform as _platform
import shutil
import sys
import tarfile
import tempfile
import time
import zipfile
from pathlib import Path

import config

logger = logging.getLogger(__name__)

MANAGED_ENV = "PAWPOLLER_SERVER_MANAGED"
ROOT_ENV = "PAWPOLLER_SERVER_ROOT"
RESTART_EXIT_CODE = 75            # "please start me again" to the service manager
ASSET_PREFIX = "PawPoller-Server-"
GITHUB_REPO = "knaughtykat01-prog/pawpoller-public"
FIRST_CHECK_DELAY = 10 * 60       # let a fresh start settle before the first check
CHECK_INTERVAL = 24 * 3600
HTTP_TIMEOUT = 30.0
KEEP_RELEASES = 2                 # current + the one before it


# ── which asset is ours ──────────────────────────────────────────────────────

def platform_tag(system: str | None = None, machine: str | None = None) -> str:
    """``linux-x86_64`` · ``linux-arm64`` · ``windows-x64`` · ``darwin-arm64`` · ``darwin-x86_64``."""
    system = (system or sys.platform).lower()
    machine = (machine or _platform.machine()).lower()
    arm = machine in ("aarch64", "arm64")
    if system.startswith("win"):
        return "windows-arm64" if arm else "windows-x64"
    if system.startswith("darwin"):
        return "darwin-arm64" if arm else "darwin-x86_64"
    return "linux-arm64" if arm else "linux-x86_64"


def archive_ext(tag: str) -> str:
    return ".zip" if tag.startswith("windows") else ".tar.gz"


def asset_name(version: str, tag: str) -> str:
    return f"{ASSET_PREFIX}{version}-{tag}{archive_ext(tag)}"


def pick_asset(assets: list, tag: str) -> tuple[str | None, str | None]:
    """(archive_url, sha256_url) for this platform from a release's asset list."""
    suffix = f"-{tag}{archive_ext(tag)}"
    archive = sha = None
    for a in assets or []:
        name = a.get("name", "")
        if not name.startswith(ASSET_PREFIX):
            continue
        if name.endswith(suffix):
            archive = a.get("browser_download_url")
        elif name.endswith(suffix + ".sha256"):
            sha = a.get("browser_download_url")
    return archive, sha


# ── the on-disk layout ───────────────────────────────────────────────────────

def managed(env: dict | None = None) -> bool:
    env = os.environ if env is None else env
    root = env.get(ROOT_ENV, "")
    return env.get(MANAGED_ENV, "") == "1" and bool(root) and Path(root).is_dir()


def root_dir(env: dict | None = None) -> Path:
    env = os.environ if env is None else env
    return Path(env.get(ROOT_ENV, "")).resolve()


def releases_dir(root: Path) -> Path:
    return root / "releases"


def current_link(root: Path) -> Path:
    return root / "current"


def current_version(root: Path) -> str | None:
    link = current_link(root)
    try:
        target = link.resolve()
    except OSError:
        return None
    if not target.exists():
        return None
    return target.name


def _point_current_at(root: Path, target: Path) -> None:
    """Replace ``current`` so it points at *target*. Symlink on POSIX; junction on Windows."""
    link = current_link(root)
    if sys.platform == "win32":
        import _winapi
        if link.exists() or link.is_symlink():
            try:
                os.rmdir(link)            # a junction is removed with rmdir, contents untouched
            except NotADirectoryError:
                link.unlink()
        _winapi.CreateJunction(str(target), str(link))
        return
    tmp = root / "current.new"
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()
    os.symlink(target, tmp, target_is_directory=True)
    os.replace(tmp, link)                 # atomic swap of the symlink itself


def switch(root: Path, version: str) -> Path:
    target = releases_dir(root) / version
    if not target.is_dir():
        raise FileNotFoundError(f"release {version} is not staged")
    _point_current_at(root, target)
    logger.info("Server self-update: current -> %s", target)
    return target


def prune(root: Path, keep: int = KEEP_RELEASES) -> list[str]:
    """Delete old releases beyond *keep* (never the current one). Returns what was removed."""
    cur = current_version(root)
    rel = releases_dir(root)
    if not rel.is_dir():
        return []
    dirs = [d for d in rel.iterdir() if d.is_dir() and not d.name.endswith(".staging")]
    dirs.sort(key=lambda d: d.stat().st_mtime, reverse=True)
    survivors = [d for d in dirs if d.name == cur][:1] + [d for d in dirs if d.name != cur][: max(0, keep - 1)]
    removed = []
    for d in dirs:
        if d not in survivors:
            shutil.rmtree(d, ignore_errors=True)
            removed.append(d.name)
    return removed


# ── download, verify, stage ──────────────────────────────────────────────────

def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_sha256_file(text: str) -> str:
    """First 64-hex token of a ``sha256sum``-style line (``<hex>  <name>``) or a bare digest."""
    for tok in (text or "").split():
        if len(tok) == 64 and all(c in "0123456789abcdefABCDEF" for c in tok):
            return tok.lower()
    return ""


def stage(archive: Path, version: str, root: Path) -> Path:
    """Unpack *archive* into ``releases/<version>``; a single top-level folder is flattened."""
    rel = releases_dir(root)
    rel.mkdir(parents=True, exist_ok=True)
    final = rel / version
    staging = rel / f"{version}.staging"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir()
    if archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive) as z:
            for m in z.infolist():
                _guard_member(m.filename, staging)
            z.extractall(staging)
    else:
        with tarfile.open(archive, "r:gz") as t:
            for m in t.getmembers():
                if m.issym() or m.islnk():
                    raise ValueError(f"archive contains a link: {m.name}")
                _guard_member(m.name, staging)
            t.extractall(staging)
    entries = [p for p in staging.iterdir()]
    if len(entries) == 1 and entries[0].is_dir():
        inner = entries[0]
        for child in inner.iterdir():
            shutil.move(str(child), str(staging / child.name))
        inner.rmdir()
    exe = "PawPoller-Server.exe" if sys.platform == "win32" else "PawPoller-Server"
    if not (staging / exe).exists():
        shutil.rmtree(staging, ignore_errors=True)
        raise FileNotFoundError(f"{exe} missing from the archive")
    if not sys.platform == "win32":
        (staging / exe).chmod(0o755)
    if final.exists():
        shutil.rmtree(final)
    staging.rename(final)
    return final


def _guard_member(name: str, base: Path) -> None:
    p = (base / name).resolve()
    if base.resolve() not in p.parents and p != base.resolve():
        raise ValueError(f"archive member escapes the target: {name}")


# ── the check ────────────────────────────────────────────────────────────────

def fetch_release(http=None) -> dict:
    """The latest GitHub release: ``{"tag_name", "assets": [...]}``."""
    import httpx
    url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
    client = http or httpx.Client(timeout=HTTP_TIMEOUT, follow_redirects=True,
                                  headers={"Accept": "application/vnd.github+json",
                                           "User-Agent": f"PawPoller-Server/{config.APP_VERSION}"})
    try:
        r = client.get(url)
        r.raise_for_status()
        return r.json()
    finally:
        if http is None:
            client.close()


def download(url: str, dest: Path, http=None) -> Path:
    import httpx
    client = http or httpx.Client(timeout=HTTP_TIMEOUT, follow_redirects=True)
    try:
        with client.stream("GET", url) as r:
            r.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in r.iter_bytes():
                    f.write(chunk)
        return dest
    finally:
        if http is None:
            client.close()


def run_once(env: dict | None = None, settings: dict | None = None, http=None, tag: str | None = None) -> str:
    """One update pass. Returns ``unmanaged`` · ``off`` · ``none`` · ``staged:<v>`` · ``failed:<why>``."""
    from updater import _version_newer
    env = os.environ if env is None else env
    if not managed(env):
        return "unmanaged"
    settings = config.get_settings() if settings is None else settings
    if settings.get("auto_update", True) in (False, "false", "0", "off"):
        return "off"
    root = root_dir(env)
    tag = tag or platform_tag()
    try:
        release = fetch_release(http)
        latest = str(release.get("tag_name", "")).lstrip("v")
        current = current_version(root) or config.APP_VERSION
        if not latest or not _version_newer(latest, current):
            return "none"
        if (releases_dir(root) / latest).is_dir():
            switch(root, latest)
            return f"staged:{latest}"
        archive_url, sha_url = pick_asset(release.get("assets", []), tag)
        if not archive_url or not sha_url:
            return f"failed:no asset for {tag} in {latest}"
        with tempfile.TemporaryDirectory(prefix="pawpoller-server-update-") as td:
            archive = download(archive_url, Path(td) / asset_name(latest, tag), http)
            sha_text = download(sha_url, Path(td) / "expected.sha256", http).read_text(encoding="utf-8")
            expected = parse_sha256_file(sha_text)
            actual = sha256_of(archive)
            if not expected or actual != expected:
                return f"failed:checksum mismatch for {archive.name}"
            stage(archive, latest, root)
        switch(root, latest)
        prune(root)
        return f"staged:{latest}"
    except Exception as e:  # noqa: BLE001 — an updater must never take the server down
        logger.warning("Server self-update failed: %s", e)
        return f"failed:{type(e).__name__}: {e}"[:200]


def loop(exit_fn=None, sleep=time.sleep) -> None:
    """Daemon-thread body for server.py: check daily; when a release is staged, exit for restart."""
    exit_fn = exit_fn or (lambda code: os._exit(code))
    sleep(FIRST_CHECK_DELAY)
    while True:
        outcome = run_once()
        logger.info("Server self-update: %s", outcome)
        if outcome.startswith("staged:"):
            logger.info("Restarting into the new build (exit %d for the service manager)", RESTART_EXIT_CODE)
            sleep(2)
            exit_fn(RESTART_EXIT_CODE)
            return
        sleep(CHECK_INTERVAL)


def request_restart(delay: float = 1.5) -> bool:
    """Used by POST /api/mirror/restart on a managed server: exit so the unit restarts us."""
    if not managed():
        return False
    import threading

    def _bye():
        time.sleep(delay)
        os._exit(RESTART_EXIT_CODE)

    threading.Thread(target=_bye, daemon=True, name="server-restart").start()
    return True
