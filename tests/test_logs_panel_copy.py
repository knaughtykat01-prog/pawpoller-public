"""The floating logs panel must hand its lines over (backlog LOGCOPY).

The panel exists so someone can send their log somewhere — but the only way to get the
text out was to drag-select it, which the live tail fought: every new line auto-scrolled
the selection away, and the desktop webview does not always offer the async clipboard.
A Copy button now takes exactly the lines the level filter leaves visible, with a
textarea + execCommand fallback, and the body is explicitly selectable.

Verified in a browser (the button copies visible lines only, skipping filtered-out ones);
these pin the pieces, in the style of test_hash_names_and_tg_chart.py.
"""
from __future__ import annotations

PANEL = open("frontend/js/logs_panel.js", encoding="utf-8").read()
CSS = open("frontend/css/loading_indicator.css", encoding="utf-8").read()


class TestTheCopyButton:
    def test_it_is_in_the_header(self):
        assert 'class="pp-logs-copy"' in PANEL
        assert ".pp-logs-copy').addEventListener('click'" in PANEL

    def test_it_copies_only_what_is_on_screen(self):
        """A filtered-out line is display:none — copying it back would confuse the report."""
        assert "filter(el => el.style.display !== 'none')" in PANEL

    def test_it_falls_back_when_the_webview_has_no_clipboard_api(self):
        assert "navigator.clipboard && navigator.clipboard.writeText" in PANEL
        assert "document.execCommand('copy')" in PANEL

    def test_it_says_what_happened(self):
        for word in ("copy failed", "empty", "✓"):
            assert word in PANEL


class TestSelectionSurvivesTheTail:
    def test_the_body_is_selectable_whatever_a_parent_says(self):
        body = CSS[CSS.index(".pp-logs-body {"):]
        assert "user-select: text" in body[:600]

    def test_new_lines_do_not_scroll_a_selection_away(self):
        assert "stickToBottom && !hasSelectionInBody()" in PANEL
        assert "sel.isCollapsed" in PANEL
