"""SETUPALPHA (4.88.1): every platform list a person scans is A–Z by name.

The frontend sorts through ``window.sortByPlatformName`` (frontend/js/platforms.js);
these are the server-side lists that pickers render in key order.
"""
import re
from pathlib import Path

from database.accounts import PLATFORM_NAMES
from routes.editor_api import PUBLISH_PLATFORMS

ROOT = Path(__file__).resolve().parent.parent


def _is_az(names):
    names = list(names)
    return names == sorted(names, key=str.lower)


def test_platform_names_are_a_to_z():
    assert _is_az(PLATFORM_NAMES.values())


def test_publish_check_platforms_are_a_to_z():
    assert _is_az(name for _, name in PUBLISH_PLATFORMS)


def test_hand_written_handle_lists_are_a_to_z():
    for rel in ("frontend/js/artists.js", "frontend/js/artist_picker.js"):
        src = (ROOT / rel).read_text(encoding="utf-8")
        block = re.search(r"PLATFORMS\s*[:=]\s*\[(.*?)\n\s*\][,;]", src, re.S).group(1)
        labels = re.findall(r"\['\w+',\s*'([^']+)'\]", block)
        assert len(labels) >= 10, rel
        assert _is_az(labels), rel


def test_helper_is_exported():
    src = (ROOT / "frontend/js/platforms.js").read_text(encoding="utf-8")
    assert "window.sortByPlatformName = sortByPlatformName" in src
