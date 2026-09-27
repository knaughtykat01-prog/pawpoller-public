"""Settings tick boxes on a phone (2026-09-27).

The mobile rule that makes text fields full-width and 44-48px tall also caught
checkboxes: each Telegram channel option became a huge white square and pushed
its label off the right edge. The rule is for text fields only.
"""
from pathlib import Path


def _css(name):
    return (Path("frontend/css") / name).read_text(encoding="utf-8")


def test_mobile_field_sizing_skips_checkboxes_and_radios():
    for name in ("layout.css", "editor.css"):
        for line in _css(name).splitlines():
            if ".settings-row input" in line and "{" in line or line.rstrip().endswith(".settings-row input,"):
                assert ':not([type="checkbox"]):not([type="radio"])' in line, (name, line.strip())


def test_the_telegram_options_get_a_finger_sized_box_on_mobile():
    assert 'html[data-mobile="1"] .tgopt input[type="checkbox"] { width: 20px; height: 20px;' in _css("components.css")
