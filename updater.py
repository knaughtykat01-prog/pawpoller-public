"""Self-contained update module for checking and applying updates from GitHub releases."""

from __future__ import annotations
import logging
import os
import sys
import tempfile
import zipfile
from pathlib import Path

import httpx

from config import APP_VERSION
import config

logger = logging.getLogger(__name__)
# Set to "owner/repo" (e.g. "user/PawPoller") to enable update checks.
# Left empty to disable update checks.
#
# ⚠ This must be a PUBLIC repo. The check is unauthenticated, so a private repo
# answers 404 — and the 404 is swallowed as "no releases available", meaning
# every user is silently told they are up to date forever. It pointed at the
# private development repo until the public distribution existed.
GITHUB_REPO = "knaughtykat01-prog/pawpoller-public"


def check_for_update() -> dict:
    """Check GitHub releases for a newer version.

    Returns dict with keys: available, current, latest, download_url, release_notes
    """
    # Early exit when no repo is configured — returns a "no update" response
    # so callers don't need to know whether update checking is enabled.
    if not GITHUB_REPO:
        return {"available": False, "current": APP_VERSION, "latest": APP_VERSION, "download_url": None, "release_notes": ""}

    try:
        # GitHub Releases API — the /releases/latest endpoint returns the most
        # recent non-prerelease, non-draft release. Auth token required for
        # private repos; read from settings.json "github_pat" key.
        headers = {"Accept": "application/vnd.github+json"}
        settings = config.get_settings()
        pat = settings.get("github_pat", "")
        if pat:
            headers["Authorization"] = f"token {pat}"

        with httpx.Client(timeout=15.0) as client:
            resp = client.get(
                f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest",
                headers=headers,
            )
            # 404 from /releases/latest is the legitimate "this repo has no
            # published releases yet" case — log it once at INFO and return a
            # clean no-update response. WARN-logging it on every dashboard
            # load (BUG-009 in 2.14.6) flooded server.log with noise during
            # the window between code release and tag publication.
            if resp.status_code == 404:
                if not getattr(check_for_update, "_logged_no_releases", False):
                    logger.info(
                        "GitHub repo %s has no published releases yet — "
                        "update checks will keep returning no-update.",
                        GITHUB_REPO,
                    )
                    check_for_update._logged_no_releases = True  # type: ignore[attr-defined]
                return {
                    "available": False,
                    "current": APP_VERSION,
                    "latest": APP_VERSION,
                    "download_url": None,
                    "release_notes": "",
                }
            resp.raise_for_status()
            data = resp.json()

        # Strip leading "v" prefix (e.g. "v1.2.0" -> "1.2.0") so both sides
        # are plain numeric semver strings for comparison.
        latest_tag = data.get("tag_name", "").lstrip("v")
        current = APP_VERSION.lstrip("v")

        # Semantic version comparison — splits on "." and compares tuples
        # numerically (see _version_newer below).
        available = _version_newer(latest_tag, current)

        # Pick the right release asset for this OS. Each tagged release
        # attaches the Windows zip, the Windows installer .exe, and the
        # Linux AppImage. The auto-updater here prefers the *upgradable*
        # asset for the current OS (zip on Windows since it slot-replaces
        # the install dir; AppImage on Linux since it replaces in place).
        # The Windows .exe installer ships for fresh installs and shows
        # up on the website, but isn't what the in-app updater pulls.
        download_url = _pick_update_asset(data.get("assets", []))

        return {
            "available": available,
            "current": APP_VERSION,
            "latest": latest_tag or APP_VERSION,
            "download_url": download_url,
            "release_notes": data.get("body", ""),
        }
    except Exception as e:
        logger.warning("Update check failed: %s", e)
        return {"available": False, "current": APP_VERSION, "latest": APP_VERSION, "download_url": None, "error": str(e)}


def download_update(download_url: str) -> Path:
    """Download the update asset to a temp dir. Returns path to downloaded file.

    The asset shape differs by OS — a .zip on Windows, an .AppImage on
    Linux. The downloaded filename uses the URL's basename so the
    extension is preserved (apply_update branches on sys.platform, not
    on filename, but a sensible name helps log triage).
    """
    temp_dir = Path(tempfile.mkdtemp(prefix="iba_update_"))
    # Best-effort filename from URL; fall back to "update.bin" for opaque URLs.
    from urllib.parse import urlparse
    url_name = Path(urlparse(download_url).path).name or "update.bin"
    zip_path = temp_dir / url_name

    # Auth header for private repo asset downloads.
    headers = {"Accept": "application/octet-stream"}
    settings = config.get_settings()
    pat = settings.get("github_pat", "")
    if pat:
        headers["Authorization"] = f"token {pat}"

    # Streaming download pattern — instead of loading the entire ZIP into memory
    # (which could be tens of MB), we stream it in 8KB chunks and write directly
    # to disk. follow_redirects=True is needed because GitHub asset URLs redirect
    # through their CDN. The generous 120s timeout accommodates slow connections.
    try:
        with httpx.Client(timeout=120.0, follow_redirects=True) as client:
            with client.stream("GET", download_url, headers=headers) as resp:
                resp.raise_for_status()
                with open(zip_path, "wb") as f:
                    for chunk in resp.iter_bytes(chunk_size=8192):
                        f.write(chunk)
    except Exception:
        # Clean up temp directory on download failure
        import shutil
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise

    logger.info("Update downloaded to %s", zip_path)
    return zip_path


def _resolve_source_dir(extract_dir: Path, exe_name: str) -> Path:
    """The tree that robocopy /MIR should mirror onto the install dir.

    Must be the folder that directly holds ``exe_name`` (+ ``_internal/``). A
    release zip may wrap those in ONE top-level folder (the PyInstaller onedir
    name, e.g. ``PawPoller/``); a flat zip has them at the root. Descend into a
    lone wrapper dir so the mirror lands ON the install dir instead of nesting the
    build under ``app_dir\\PawPoller\\`` while /MIR purges the real ``_internal`` —
    the corruption that shipped a schema.sql-less install in ≤2.162.0.

    Raises if the resolved tree has no executable — better to abort than let /MIR
    purge a working install from a malformed payload.
    """
    source = extract_dir
    entries = list(extract_dir.iterdir())
    if len(entries) == 1 and entries[0].is_dir():
        source = entries[0]
    if not (source / exe_name).is_file():
        raise RuntimeError(
            f"Update payload missing {exe_name} at its root ({source}); "
            f"aborting to avoid corrupting the install.")
    return source


# Seconds the update .bat waits before mirroring the new build in — long enough
# for this process to exit and release the lock on its own .exe/DLLs. The caller
# (routes/api.py::apply_update) schedules os._exit shortly after it responds, so
# 5s leaves a comfortable margin. robocopy /MIR against a still-locked exe only
# partial-copies (new _internal, stale exe) — the 3.0.0 incident.
_UPDATE_EXIT_GRACE_SECONDS = 5


def _build_update_bat(source_dir, app_dir, exe_name: str) -> str:
    """The self-update batch script (Windows). The running .exe can't overwrite
    itself, so this detached script:
      1. `timeout` — waits _UPDATE_EXIT_GRACE_SECONDS for this process to exit
         and release the file locks on its own .exe and DLLs.
      2. `robocopy /MIR` — mirrors the extracted update onto the install dir,
         replacing all files (and purging removed ones).
      3. `/XD data logs` — SAFETY: never touches the user's database/config/logs.
      4. `start` — relaunches the updated .exe.
      5. `del "%~f0"` — the script deletes itself.
    /R:2 /W:2 bounds robocopy's retries (its default is /R:1000000 /W:30, which
    would hang the updater forever on a single AV-locked file)."""
    return f"""@echo off
timeout /t {_UPDATE_EXIT_GRACE_SECONDS} /nobreak > nul
robocopy "{source_dir}" "{app_dir}" /MIR /XD data logs /R:2 /W:2 /NP
start "" "{app_dir}\\{exe_name}"
del "%~f0"
"""


def apply_update(zip_path: Path) -> None:
    """Apply the downloaded update.

    Windows: extract the zip + write a batch script that mirrors the
    extracted tree into the install dir and restarts.
    Linux: hand off to _apply_update_linux (in-place AppImage replace).
    """
    # Auto-update only works in frozen (PyInstaller) builds because:
    # 1. In dev, files are spread across source directories and venvs — robocopy
    #    would clobber the dev environment.
    # 2. sys.executable points to the .exe in frozen builds, but to the Python
    #    interpreter in dev — so the restart command would be wrong.
    # 3. Developers should update via git pull, not self-update.
    if not getattr(sys, "frozen", False):
        raise RuntimeError("Auto-update is only supported in frozen builds")

    # Linux: the "zip_path" is actually the AppImage file (download_update
    # writes whatever the asset is — see _pick_update_asset).
    if sys.platform.startswith("linux"):
        _apply_update_linux(zip_path)
        return

    # Extract the downloaded ZIP into a sibling "extracted" folder inside temp.
    # Validate all paths before extraction to prevent Zip Slip attacks
    # (malicious entries with "../" that write outside the target directory).
    extract_dir = zip_path.parent / "extracted"
    with zipfile.ZipFile(zip_path, "r") as zf:
        for member in zf.namelist():
            member_path = (extract_dir / member).resolve()
            if not str(member_path).startswith(str(extract_dir.resolve())):
                raise RuntimeError(f"Zip Slip detected: {member} escapes extract directory")
        zf.extractall(extract_dir)

    app_dir = Path(sys.executable).parent
    exe_name = Path(sys.executable).name

    source_dir = _resolve_source_dir(extract_dir, exe_name)

    bat_path = zip_path.parent / "_update.bat"
    bat_path.write_text(_build_update_bat(source_dir, app_dir, exe_name), encoding="utf-8")
    logger.info("Update script written to %s", bat_path)

    # os.startfile launches the .bat asynchronously (detached from this process).
    # The .bat waits _UPDATE_EXIT_GRACE_SECONDS, so the CALLER MUST exit the app
    # within that window to release the lock on its own .exe/DLLs — otherwise
    # robocopy hits the running, locked exe and only partial-copies (the 3.0.0
    # in-app-update incident). routes/api.py::apply_update schedules that exit.
    os.startfile(str(bat_path))


def spawn_relauncher() -> None:
    """Restart this app WITHOUT changing any files (3.18.0).

    The mirror stages its database snapshot as `pawpoller.db.pending` and
    `init_db()` applies it on the way up, because SQLite cannot be replaced
    under a live app on Windows. That constraint is real and stays — but until
    now it surfaced to the user as an instruction ("pull, quit, relaunch")
    rather than a button.

    This is `apply_update`'s relaunch shorn of the update: no zip, no robocopy,
    no file replacement — just wait for this process to die and start it again.
    It lives HERE, beside `_build_update_bat` and `_apply_update_linux`, because
    this module is already where "how to relaunch this app on this platform"
    is written down, and a second copy elsewhere is how the two drift.

    The CALLER must exit within the grace window; the helper only waits.
    """
    if not getattr(sys, "frozen", False):
        # In dev, sys.executable is the interpreter, so "start it again" would
        # launch a bare Python REPL and the app would simply be gone.
        raise RuntimeError("Restart is only supported in frozen builds")

    exe = Path(sys.executable)
    if sys.platform.startswith("linux"):
        # shlex.quote for the same reason `_apply_update_linux` does it below:
        # the install path is the user's choice and may legally contain quotes,
        # backticks or `$`. Inside a bash double-quoted string `$(…)`, backticks
        # and `$VAR` still expand, so an unquoted path is executed rather than
        # run. This function was written as that one's relaunch half and had
        # dropped the hardening — diverging silently from a convention the file
        # documents is how the next author concludes it was never needed.
        import shlex
        _exe = shlex.quote(str(exe))
        script = exe.parent / "_relaunch.sh"
        script.write_text(f"""#!/usr/bin/env bash
# PawPoller relaunch — generated by updater.spawn_relauncher
sleep {_UPDATE_EXIT_GRACE_SECONDS}
exec {_exe}
""", encoding="utf-8")
        script.chmod(0o755)
        import subprocess
        subprocess.Popen(["bash", str(script)],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
        return

    # `%` is legal in an NTFS filename and cmd expands `%VAR%` even INSIDE
    # double quotes, so quoting alone does not protect the Windows branch —
    # doubling is what escapes a literal percent in a batch file.
    bat = exe.parent / "_relaunch.bat"
    _exe = str(exe).replace("%", "%%")
    bat.write_text(f"""@echo off
timeout /t {_UPDATE_EXIT_GRACE_SECONDS} /nobreak > nul
start "" "{_exe}"
del "%~f0"
""", encoding="utf-8")
    logger.info("Relaunch script written to %s", bat)
    os.startfile(str(bat))


_LINUX_ASSET_SUFFIX = "-x86_64.AppImage"
_WINDOWS_ASSET_SUFFIX = ".zip"


def _pick_update_asset(assets: list) -> str | None:
    """Return the browser_download_url for the right asset on this OS.

    Per-OS preference:
      Linux   -> *-x86_64.AppImage (one file, in-place replace)
      Windows -> *.zip             (extract + robocopy mirror)

    The Windows installer .exe is intentionally NOT chosen here — the
    in-app updater is for incremental upgrades of an already-installed
    app, and running the installer would either UAC-prompt or no-op
    (it's already installed). Fresh-install users grab the .exe from
    the website / Releases page directly.
    """
    suffix = _LINUX_ASSET_SUFFIX if sys.platform.startswith("linux") else _WINDOWS_ASSET_SUFFIX
    for asset in assets:
        name = asset.get("name", "")
        # 4.12.0: the release also carries PawPoller-Server-* archives (the headless build).
        # On Windows both end in .zip — picking the server build would replace the desktop
        # app with a console server and there would be no window to notice it.
        if name.startswith("PawPoller-Server-"):
            continue
        if name.endswith(suffix):
            # Skip the Windows installer (.exe) — handled by the suffix
            # match above (the suffix is .zip on Windows, so .exe assets
            # are naturally excluded). Defensive check anyway.
            if sys.platform == "win32" and name.endswith("-Setup.exe"):
                continue
            return asset.get("browser_download_url")
    return None


def _apply_update_linux(appimage_path: Path) -> None:
    """In-place replace the running AppImage with the downloaded one.

    AppImage's standard `APPIMAGE` env var carries the absolute path of
    the .AppImage file currently executing. We replace that file with
    the freshly downloaded one and re-exec.

    Spawns a detached shell script so the running process can exit
    cleanly before the move; the script then runs the new AppImage.
    """
    current = os.environ.get("APPIMAGE")
    if not current:
        raise RuntimeError(
            "Cannot find APPIMAGE env var — auto-update only works when "
            "running as an AppImage. Re-download manually from Releases."
        )
    current_path = Path(current)

    # shlex.quote both paths: $APPIMAGE is the user's chosen filename and
    # may legally contain quotes/backticks/$ — unquoted they'd break out of
    # the mv/exec lines in the generated script.
    import shlex
    _new = shlex.quote(str(appimage_path))
    _cur = shlex.quote(str(current_path))

    # tempdir is created by download_update one level up from appimage_path.
    script_path = appimage_path.parent / "_update.sh"
    script_content = f"""#!/usr/bin/env bash
# PawPoller AppImage self-update — generated by updater.py
set -e
sleep 2
mv -f -- {_new} {_cur}
chmod +x -- {_cur}
exec {_cur}
"""
    script_path.write_text(script_content, encoding="utf-8")
    script_path.chmod(0o755)
    logger.info("Update script written to %s", script_path)

    # Detach: spawn the script with stdin/stdout/stderr closed and
    # setsid so it survives the parent exiting.
    import subprocess
    subprocess.Popen(
        ["bash", str(script_path)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _version_newer(latest: str, current: str) -> bool:
    """Compare semantic version strings. Returns True if latest > current."""
    try:
        # Split "1.2.3" into (1, 2, 3) tuples. Python's tuple comparison is
        # lexicographic over elements, which matches semver ordering:
        #   (1, 2, 3) > (1, 2, 2)  -> True  (patch bump)
        #   (2, 0, 0) > (1, 9, 9)  -> True  (major bump)
        # Falls back to False on malformed version strings (non-numeric parts).
        lat = tuple(int(x) for x in latest.split("."))
        cur = tuple(int(x) for x in current.split("."))
        return lat > cur
    except (ValueError, AttributeError):
        return False
