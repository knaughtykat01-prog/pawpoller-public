"""The Settings credential rows must be able to say "expired" (4.0.11).

Six rows rendered a hardcoded "Connected — tracking" whenever credentials
merely existed. `has_credentials` is presence, never validity, so cookies six
months dead rendered identically to fresh ones while every poll failed and the
tray filled with "credential validation failed". A tester read that row as
"it says connected, so why doesn't it work" — which is the only thing it could
ever have said.

The fix reads the session verdict the same page's session-health card already
shows, and says "not verified" honestly for the platforms nothing checks.
docs/specs/status_and_sort.md §1.
"""
from __future__ import annotations

import re

# Every platform whose Settings row hardcoded "Connected — tracking". Fifteen,
# not the six first reported: the survey grep only caught rows whose gate sat
# within a few lines. Nine of these are in session_check.CHECKABLE — the app
# HAD a verdict and the row ignored it.
SIX = ("sqw", "ao3", "da", "wp", "ik", "bsky", "mast", "tum", "pix", "thr",
       "ig", "tw", "e621", "fn", "fbr")


def _src():
    return open("frontend/js/app.js", encoding="utf-8").read()


def test_no_row_asserts_connected_from_mere_presence():
    """THE bug. A status string gated only on has_credentials cannot express
    failure. Grep for the literal that shipped, in the shape that shipped."""
    src = _src()
    assert 'telegram-status connected">Connected — tracking ${Utils.escapeHtml(' not in src, (
        "a Settings row still hardcodes 'Connected' from has_credentials")


def test_all_six_rows_go_through_the_helper():
    src = _src()
    for code in SIX:
        assert f"this._credStatus('{code}'," in src, f"{code} row bypasses _credStatus"


def test_the_summary_dot_agrees_with_the_row():
    """A green dot beside a red row is a second lie. Both read the same
    verdict, so they cannot disagree."""
    src = _src()
    for code in SIX:
        assert re.search(rf"status-dot \$\{{{code}Auth\.has_credentials \? this\._credStatus\('{code}'", src), (
            f"{code}: summary dot still derives from has_credentials alone")


def test_the_helper_has_all_four_states_and_an_honest_default():
    """`valid` / `expired` / `error` / everything else. The fourth is the one
    that matters: for da/wp/ik/tw nothing validates the session, and the
    honest statement is "saved, not verified" — not "Connected"."""
    src = _src()
    i = src.index("_credStatus(code, username) {")
    block = src[i:i + 1600]
    for state in ("'valid'", "'expired'", "'error'"):
        assert f"sess === {state}" in block, f"missing state {state}"
    assert "not verified" in block, "the unverified default must say so in words"
    assert "'muted'" in block and "'warn'" in block


def test_the_last_poll_error_is_surfaced_in_the_row():
    """The poll log already stored it and /api/platforms/health already
    returned it; it only ever appeared in a dismissable notification. The
    row is where the user is looking."""
    src = _src()
    i = src.index("_credStatus(code, username) {")
    block = src[i:i + 1600]
    assert "last_poll_error" in block
    assert "last_poll_status === 'error'" in block


def test_the_two_new_states_are_styled():
    """A class with no rule renders as an unstyled span — the row would read
    correctly and look broken, which is its own kind of wrong."""
    css = open("frontend/css/components.css", encoding="utf-8").read()
    assert ".telegram-status.warn" in css
    assert ".telegram-status.muted" in css


def test_it_reads_the_same_source_as_the_session_health_card():
    """Two vocabularies for one fact on one page is a bug in waiting. The
    card's DOT map is the canonical one; the helper must map the same four
    session states to the same class names."""
    src = _src()
    i = src.index("const DOT = {")
    dot = src[i:src.index("}", i) + 1]
    for state, cls in (("valid", "connected"), ("expired", "disconnected"),
                       ("error", "warn"), ("unconfigured", "muted")):
        assert f"{state}: '{cls}'" in dot, "the card's map changed; keep _credStatus in step"
    j = src.index("_credStatus(code, username) {")
    block = src[j:j + 1600]
    assert "cls = 'connected'" in block and "cls = 'disconnected'" in block
    assert "cls = 'warn'" in block and "cls = 'muted'" in block
