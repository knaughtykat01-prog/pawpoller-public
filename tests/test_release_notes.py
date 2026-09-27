"""The release page says which file to download (2026-09-27).

GitHub lists a release's ~12 files alphabetically, so the Windows installer sits
9th under server packages and checksums. installer/changelog_extract.py puts a
plain "Which file do I download?" table at the top of every release body. It is
only useful if the names in it are the names build.yml really uploads.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _notes(tmp_path, version="9.8.7", changelog=None):
    if changelog is not None:
        (tmp_path / "CHANGELOG.md").write_text(changelog, encoding="utf-8")
    subprocess.run([sys.executable, str(ROOT / "installer" / "changelog_extract.py"), version],
                   cwd=tmp_path, check=True, capture_output=True)
    return (tmp_path / "RELEASE_NOTES.md").read_text(encoding="utf-8")


def test_the_public_release_page_opens_with_which_file(tmp_path):
    """The public repo has no CHANGELOG: the box is most of the page there."""
    notes = _notes(tmp_path)
    assert notes.startswith("### Which file do I download?")
    assert "`PawPoller-Setup-9.8.7.exe`" in notes and "`PawPoller-9.8.7-x86_64.AppImage`" in notes
    assert "Not available yet" in notes


def test_the_changelog_entry_follows_the_box(tmp_path):
    notes = _notes(tmp_path, changelog="# Log\n\n## [9.8.7] - x - y\n\n> - A change.\n\n---\n\n## [9.8.6] - old\n")
    assert notes.index("Which file do I download?") < notes.index("> - A change.")
    assert "9.8.6" not in notes


def test_the_named_files_are_the_files_ci_uploads():
    wf = (ROOT / ".github" / "workflows" / "build.yml").read_text(encoding="utf-8")
    assert "installer/Output/PawPoller-Setup-*.exe" in wf
    assert "installer/Output/PawPoller-*-x86_64.AppImage" in wf
    assert "dist/PawPoller-windows-x64.zip" in wf
