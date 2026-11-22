"""Self-updater source-dir resolution (2.162.1).

Regression guard for the update that shipped a corrupted, non-launching install
(missing `_internal/database/schema.sql`). The release zip wrapped the build in a
top-level `PawPoller/` folder, but the updater robocopy-/MIR'd the extract ROOT
onto the install dir — nesting the new build AND purging the real `_internal`.
`_resolve_source_dir` descends into a lone wrapper so the mirror lands correctly,
and refuses a payload with no executable rather than purge a working install.
"""
import shutil
import subprocess
import sys

import pytest

from updater import _resolve_source_dir, _build_update_bat, _UPDATE_EXIT_GRACE_SECONDS

EXE = "PawPoller.exe"


def test_update_bat_waits_for_exit_relaunches_and_spares_user_data():
    """The self-update .bat must (a) wait long enough for this process to exit
    and release its exe/DLL locks — the 3.0.0 incident was robocopy running
    against a still-running, locked PawPoller.exe — (b) never touch data/logs,
    and (c) relaunch + self-delete."""
    bat = _build_update_bat("C:\\src", "C:\\app", EXE)
    assert _UPDATE_EXIT_GRACE_SECONDS >= 3, "grace must outlast the app's ~1.5s self-exit"
    assert f"timeout /t {_UPDATE_EXIT_GRACE_SECONDS}" in bat
    assert 'robocopy "C:\\src" "C:\\app" /MIR /XD data logs' in bat   # never the user's DB/logs
    assert 'start "" "C:\\app\\PawPoller.exe"' in bat                  # relaunch the updated exe
    assert 'del "%~f0"' in bat                                          # script cleans itself up


@pytest.mark.skipif(sys.platform != "win32" or not shutil.which("robocopy"), reason="robocopy is Windows-only")
def test_an_update_keeps_a_setup_copys_uninstaller(tmp_path):
    """The zip carries no Inno uninstaller, so /MIR deleted it on a Setup copy's first update: Apps & features
    could no longer uninstall it, and the Start menu repair (keyed on it then) skipped it (4.92.5, UPDUNINS)."""
    src, app = tmp_path / "new", tmp_path / "Program Files & co"
    for d, body in ((src, b"new build"), (app, b"old")):   # sizes differ: robocopy skips same size + time
        d.mkdir()
        (d / EXE).write_bytes(body)
    for name in ("unins000.exe", "unins000.dat", "data/db"):
        (app / name).parent.mkdir(exist_ok=True)
        (app / name).write_bytes(b"keep")
    line = next(l for l in _build_update_bat(str(src), str(app), EXE).splitlines() if l.startswith("robocopy"))
    assert subprocess.run(line, shell=True, capture_output=True, timeout=60).returncode < 8   # robocopy: <8 = ok
    assert (app / EXE).read_bytes() == b"new build"
    assert all((app / n).read_bytes() == b"keep" for n in ("unins000.exe", "unins000.dat", "data/db"))


def _extract(tmp_path, layout):
    """Build a clean 'extracted' dir under tmp_path from relative paths.

    A dedicated subdir, NOT tmp_path itself — the autouse _isolated_db fixture
    drops test.db/settings into tmp_path, and this mirrors the real updater
    (which extracts into its own `extracted/` folder) anyway.
    """
    root = tmp_path / "extracted"
    root.mkdir()
    for rel in layout:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x", encoding="utf-8")
    return root


def test_wrapped_zip_descends_into_the_single_folder(tmp_path):
    # The broken shape: everything under one `PawPoller/` wrapper.
    root = _extract(tmp_path, ["PawPoller/PawPoller.exe",
                               "PawPoller/_internal/database/schema.sql"])
    src = _resolve_source_dir(root, EXE)
    assert src == root / "PawPoller"
    assert (src / EXE).is_file()               # mirror source now holds the exe


def test_flat_zip_used_as_is(tmp_path):
    # The fixed shape: contents at the root, no wrapper.
    root = _extract(tmp_path, ["PawPoller.exe", "_internal/database/schema.sql"])
    assert _resolve_source_dir(root, EXE) == root


def test_single_wrapper_without_exe_is_rejected(tmp_path):
    # A lone folder that ISN'T the app (no exe inside) must not be mirrored —
    # /MIR would purge the real install and replace it with junk.
    root = _extract(tmp_path, ["docs/readme.txt"])
    with pytest.raises(RuntimeError, match="missing PawPoller.exe"):
        _resolve_source_dir(root, EXE)


def test_flat_payload_without_exe_is_rejected(tmp_path):
    root = _extract(tmp_path, ["_internal/database/schema.sql"])   # no exe anywhere
    with pytest.raises(RuntimeError, match="missing PawPoller.exe"):
        _resolve_source_dir(root, EXE)


def test_multiple_top_level_entries_stay_flat(tmp_path):
    # More than one top-level entry => not a wrapper; use the root (the exe is
    # here, so it's a valid flat payload).
    root = _extract(tmp_path, ["PawPoller.exe", "_internal/x.pyd", "assets/icon.ico"])
    assert _resolve_source_dir(root, EXE) == root
