"""PawPoller in-app uninstall flow.

Driven by Settings → General → Danger zone → "Uninstall PawPoller".

Goal: regardless of how the user installed (Windows installer, portable
zip, Linux AppImage, dev), one button cleanly removes:
  - Application files
  - User data (database, settings, vault, logs) — optional
  - Autostart entry — optional
  - Vault encryption key from the OS keyring — optional

The actual cleanup runs as a detached shell script (.bat / .sh) so the
running PawPoller process can exit and release file locks before the
script touches the install dir.

Self-deletion is split per install type:
  Windows installer -> delegates to unins000.exe /SILENT (same uninstaller
                       Windows Search / Apps & features invokes).
  Windows portable  -> our own .bat removes the install dir + data dir.
  Linux AppImage    -> our own .sh removes the .AppImage + data dir.
  Dev mode          -> refuses to delete the source tree (won't nuke a
                       developer's working copy); cleans data + autostart
                       only and logs a note to delete the source manually.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

import config

logger = logging.getLogger(__name__)


class InstallType(Enum):
    """How PawPoller is installed on this machine."""
    WINDOWS_INSTALLER = "windows_installer"   # via Inno Setup, has unins000.exe
    WINDOWS_PORTABLE  = "windows_portable"    # PyInstaller --onedir from the zip
    LINUX_APPIMAGE    = "linux_appimage"      # .AppImage with APPIMAGE env var set
    DEV               = "dev"                 # `python main.py` from source
    UNKNOWN           = "unknown"


@dataclass
class UninstallPlan:
    """What we're about to delete + how. Returned by detect() for the UI to display."""
    install_type: InstallType
    app_path: str           # exe / AppImage / install dir — human-readable
    data_dir: str           # %APPDATA%\PawPoller or equivalent
    autostart_target: str   # registry key or .desktop file path
    has_keyring_key: bool   # True if a vault key likely exists in the OS keyring


def _windows_install_dir() -> Path | None:
    """Return the directory containing the running PawPoller.exe, or None in dev."""
    if not getattr(sys, "frozen", False):
        return None
    return Path(sys.executable).parent


def detect() -> UninstallPlan:
    """Figure out the install type and the paths we'd touch.

    Pure / no side effects — safe to call from the UI for the confirm dialog.
    """
    install_type = InstallType.UNKNOWN
    app_path = "(unknown)"
    autostart_target = "(not configured)"

    if sys.platform == "win32":
        install_dir = _windows_install_dir()
        if install_dir is None:
            install_type = InstallType.DEV
            app_path = str(Path(__file__).resolve().parent)
        else:
            # Inno Setup writes its uninstaller next to the .exe as unins000.exe.
            # Its presence is the most reliable marker of an installer-based install.
            if (install_dir / "unins000.exe").exists():
                install_type = InstallType.WINDOWS_INSTALLER
            else:
                install_type = InstallType.WINDOWS_PORTABLE
            app_path = str(install_dir)
        autostart_target = r"HKCU\Software\Microsoft\Windows\CurrentVersion\Run\PawPoller"

    elif sys.platform.startswith("linux"):
        appimage = os.environ.get("APPIMAGE")
        if appimage:
            install_type = InstallType.LINUX_APPIMAGE
            app_path = appimage
        elif getattr(sys, "frozen", False):
            # Frozen but no APPIMAGE — could be a raw PyInstaller --onedir run
            # outside the AppImage runtime. Treat as portable for cleanup.
            install_type = InstallType.WINDOWS_PORTABLE  # same model — delete the dir
            app_path = str(Path(sys.executable).parent)
        else:
            install_type = InstallType.DEV
            app_path = str(Path(__file__).resolve().parent)
        autostart_target = str(_linux_autostart_path_safe())

    # macOS will land here as UNKNOWN until the native app ships.

    # Vault key presence is a best-effort check — keyring lib may not be
    # importable in every environment. Treat exceptions as "no key".
    has_keyring_key = False
    try:
        import keyring
        # We don't read the key value (could prompt a credential dialog);
        # we just check whether the service knows about us.
        cred = keyring.get_credential("PawPoller", "vault_key")
        has_keyring_key = cred is not None
    except Exception:
        pass

    return UninstallPlan(
        install_type=install_type,
        app_path=app_path,
        data_dir=str(config.APPDATA_DIR),
        autostart_target=autostart_target,
        has_keyring_key=has_keyring_key,
    )


def _linux_autostart_path_safe() -> Path:
    """Mirror config._linux_autostart_path without importing private helpers."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "autostart" / "PawPoller.desktop"


def _remove_keyring_entry() -> None:
    """Best-effort vault-key deletion from the OS keyring.

    Windows: Credential Manager. Linux: Secret Service (gnome-keyring /
    kwallet). Silently swallows exceptions — keyring lib may not be
    available on every install, and a missing entry is the success case.
    """
    try:
        import keyring
        keyring.delete_password("PawPoller", "vault_key")
        logger.info("Removed vault key from OS keyring")
    except Exception as e:
        logger.debug("Vault key cleanup skipped: %s", e)


def execute(remove_data: bool, remove_autostart: bool, remove_app: bool) -> dict:
    """Kick off the uninstall.

    Steps:
      1. Remove autostart entry (synchronous — quick).
      2. Remove keyring vault key (synchronous — quick).
      3. Build a detached cleanup script that:
           - waits for our process to exit (so file locks release)
           - removes the app files (per install type)
           - removes the data dir (if remove_data was requested)
      4. Spawn the script detached.
      5. Return — caller is responsible for sys.exit(0) so the script can proceed.

    Returns a dict describing what was queued, suitable for the API response
    before the app shuts down.
    """
    plan = detect()
    actions: list[str] = []

    # Step 1: autostart — handled synchronously by config.set_run_on_startup,
    # which already knows how to remove both Windows registry and Linux .desktop.
    if remove_autostart:
        try:
            config.set_run_on_startup(False)
            actions.append("autostart removed")
        except Exception as e:
            logger.warning("Failed to remove autostart entry: %s", e)
            actions.append(f"autostart removal failed: {e}")

    # Step 2: keyring vault key. Synchronous, fast.
    if remove_data:
        _remove_keyring_entry()
        actions.append("keyring entry removed (if present)")

    # Step 3 + 4: detached cleanup script. Built per install type.
    if remove_app or remove_data:
        script_path = _build_cleanup_script(
            plan=plan,
            remove_data=remove_data,
            remove_app=remove_app,
        )
        _spawn_detached(script_path)
        actions.append(f"cleanup script spawned: {script_path}")

    return {
        "ok": True,
        "install_type": plan.install_type.value,
        "actions": actions,
        "data_dir": plan.data_dir,
        "app_path": plan.app_path,
    }


def _build_cleanup_script(
    *,
    plan: UninstallPlan,
    remove_data: bool,
    remove_app: bool,
) -> Path:
    """Write the per-OS cleanup script to a temp file and return its path."""
    tempdir = Path(tempfile.mkdtemp(prefix="pp_uninstall_"))

    if sys.platform == "win32":
        return _build_windows_script(plan, tempdir, remove_data, remove_app)
    if sys.platform.startswith("linux"):
        return _build_linux_script(plan, tempdir, remove_data, remove_app)
    raise RuntimeError(f"Uninstall not yet supported on {sys.platform}")


def _build_windows_script(plan: UninstallPlan, tempdir: Path, remove_data: bool, remove_app: bool) -> Path:
    """Build a .bat script for Windows uninstall."""
    install_dir = Path(plan.app_path)
    data_dir = Path(plan.data_dir)

    lines: list[str] = [
        "@echo off",
        ":: PawPoller uninstall helper — generated by uninstall.py",
        ":: Wait for the PawPoller process to release file locks.",
        "timeout /t 3 /nobreak > nul",
        "taskkill /F /IM PawPoller.exe > nul 2>&1",
        "timeout /t 1 /nobreak > nul",
    ]

    if remove_app:
        if plan.install_type == InstallType.WINDOWS_INSTALLER:
            # Delegate to the Inno uninstaller — same code path as Windows Search
            # → Uninstall / Apps & features → Uninstall. /SILENT skips the
            # confirm dialog (the user already confirmed in-app); the Inno
            # script's CurUninstallStepChanged still prompts about user data,
            # so we pre-emptively delete %APPDATA%\PawPoller first if requested
            # to keep behaviour consistent with the in-app checkboxes.
            unins = install_dir / "unins000.exe"
            lines.append(f'"{unins}" /SILENT')
        elif plan.install_type == InstallType.WINDOWS_PORTABLE:
            # Self-delete the install dir. Have to be careful — the .bat is
            # presumably NOT inside the install dir (it's in a temp dir), so
            # rmdir is safe. Defensive guard against deleting C:\ or similar.
            lines.append(f'if exist "{install_dir}\\PawPoller.exe" (')
            lines.append(f'  rmdir /S /Q "{install_dir}"')
            lines.append(f')')
        # DEV: no app deletion — refuse to nuke a developer's source tree.

    if remove_data:
        lines.append(f'if exist "{data_dir}" rmdir /S /Q "{data_dir}"')

    # Self-delete the helper script itself.
    lines.append('del "%~f0"')

    script_path = tempdir / "uninstall.bat"
    script_path.write_text("\r\n".join(lines), encoding="utf-8")
    return script_path


def _build_linux_script(plan: UninstallPlan, tempdir: Path, remove_data: bool, remove_app: bool) -> Path:
    """Build a .sh script for Linux uninstall."""
    import shlex
    # shlex.quote every interpolated path: app_path comes from $APPIMAGE —
    # the user's chosen filename — and Linux filenames may legally contain
    # quotes/backticks/$ that would otherwise break out of the rm line.
    app_path = shlex.quote(str(plan.app_path))  # .AppImage file (or install dir for raw frozen)
    data_dir = shlex.quote(str(plan.data_dir))
    autostart = shlex.quote(str(plan.autostart_target))

    lines: list[str] = [
        "#!/usr/bin/env bash",
        "# PawPoller uninstall helper — generated by uninstall.py",
        "# Wait for the PawPoller process to release file locks.",
        "sleep 3",
        "pkill -f PawPoller || true",
        "sleep 1",
    ]

    # Autostart already removed synchronously by config.set_run_on_startup,
    # but tidy up the .desktop file defensively in case the sync call failed.
    if remove_app or remove_data:
        lines.append(f'rm -f -- {autostart}')

    if remove_app:
        if plan.install_type == InstallType.LINUX_APPIMAGE:
            lines.append(f'rm -f -- {app_path}')
        # DEV / WINDOWS_PORTABLE (rare on Linux): skip.

    if remove_data:
        lines.append(f'rm -rf -- {data_dir}')

    # Self-delete the helper script.
    lines.append('rm -f -- "$0"')

    script_path = tempdir / "uninstall.sh"
    script_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    script_path.chmod(0o755)
    return script_path


def _spawn_detached(script_path: Path) -> None:
    """Run the cleanup script detached so it survives the parent exiting."""
    if sys.platform == "win32":
        # os.startfile is the standard "launch detached" path on Windows; the
        # process won't be a child of ours and won't be killed when we exit.
        os.startfile(str(script_path))
        return

    # Linux / POSIX — Popen with start_new_session detaches from our session,
    # stdin/stdout/stderr redirected to /dev/null so the parent can exit cleanly.
    subprocess.Popen(
        ["bash", str(script_path)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
