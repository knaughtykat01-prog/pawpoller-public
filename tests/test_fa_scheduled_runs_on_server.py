"""Scheduled FurAffinity jobs never reached the server (3.26.0).

Reported: *"I scheduled a heap of fa posts but they didnt post? If we can post on
server now why did they wait? Its stories"*

They waited because `requires_mode = "desktop"` was still set on the FA poster,
and that flag does **two unrelated jobs**:

  1. the manager's post-failure handoff — try the server, queue for desktop if
     it fails; and
  2. the value stamped into `posting_queue.requires` when a job is **scheduled**
     (`routes/editor_api.py`), which the scheduler then filters on in SQL:
     `requires IN ('any', <runtime_mode>)`.

3.20.2 kept the flag on the strength of (1), documenting it as *"FAILURE-RECOVERY
ROUTING, not a hard block — the server always attempts the post first"*. That
sentence is false for scheduled work, which is most of it: under (2) the server
never attempts anything, because the row is invisible to its scheduler. The job
just waits for a desktop that may never open.

Measured on prod 2026-08-23 — one artwork scheduled to eight platforms in a
single batch, every row timed for 11:30:

    ib · sf · bsky · ik · da · e621 · fn   requires='any'      -> all completed
    fa                                     requires='desktop'  -> still pending

Twelve jobs were stranded that way, including queue #3709 from 24 July — which
the old comment had already warned about: *"if it strands jobs again, the right
fix is honest errors, not a bigger queue."*

"any" does not mean "server only" — either instance may run the job. What is
gone is the routing that stopped the server from trying.
"""
from __future__ import annotations

import pytest

from database import posting_queries
from database.db import get_connection


def _poster(platform):
    from posting.manager import _get_poster
    return _get_poster(platform)


# ── the flag ─────────────────────────────────────────────────────────

def test_furaffinity_is_reachable_from_the_server():
    assert _poster("fa").requires_mode == "any", (
        "a 'desktop' requires_mode makes SCHEDULED FA jobs invisible to the "
        "server scheduler — they wait for a desktop that may never open")


def test_no_platform_is_desktop_only():
    """The general form. Any platform set to 'desktop' strands its scheduled
    work whenever the desktop app is closed, which is the normal state of a
    server-hosted install. Setting one demands evidence that the server truly
    cannot reach it — the FA claim did not survive being tested."""
    from pathlib import Path
    import re

    offenders = []
    for f in (Path(__file__).resolve().parent.parent / "posting" / "platforms").glob("*.py"):
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            code = line.split("#", 1)[0]
            if re.search(r"requires_mode\s*=\s*[\"']desktop[\"']", code):
                offenders.append(f.name)
    assert offenders == [], (
        f"{offenders} declare requires_mode='desktop' — read the comment in "
        "base.py first: the flag also gates SCHEDULING, not just failure recovery")


# ── the mechanism that actually stranded the jobs ────────────────────

def test_a_scheduled_row_is_stamped_from_requires_mode():
    """Pins the coupling that made the flag more than failure-recovery routing.

    If this ever stops being true the flag becomes what 3.20.2 believed it was —
    but while it IS true, `requires_mode` decides whether the server may run
    scheduled work at all.
    """
    import re
    from pathlib import Path

    src = (Path(__file__).resolve().parent.parent / "routes" / "editor_api.py").read_text(
        encoding="utf-8", errors="replace")
    assert re.search(r"requires\s*=\s*getattr\(\s*poster", src) or \
        re.search(r"requires=getattr\(posters\[platform\]", src), \
        "scheduling no longer derives `requires` from requires_mode — if that is " \
        "deliberate, this test and the comments in base.py/furaffinity.py need updating"


def test_the_server_scheduler_skips_desktop_rows():
    """Why a stamped row goes nowhere: the filter is SQL, not a runtime check,
    so the job is never even considered."""
    conn = get_connection()
    try:
        posting_queries.add_to_queue(conn, "Server_Job", 1, "fa", "post",
                                     account_id=2, requires="any")
        posting_queries.add_to_queue(conn, "Desktop_Job", 1, "fa", "post",
                                     account_id=2, requires="desktop")
        conn.commit()

        names = {r["story_name"] for r in
                 posting_queries.get_pending_queue(conn, limit=20, runtime_mode="server")}
        assert "Server_Job" in names
        assert "Desktop_Job" not in names, "the server must not claim desktop-only work"

        both = {r["story_name"] for r in
                posting_queries.get_pending_queue(conn, limit=20, runtime_mode="desktop")}
        assert both == {"Server_Job", "Desktop_Job"}, \
            "'any' work must still be runnable on the desktop"
    finally:
        conn.close()


def test_an_fa_job_scheduled_now_would_run_on_the_server():
    """End to end over the real stamping rule: take FA's requires_mode, stamp a
    row with it the way the scheduling endpoint does, and confirm the server
    scheduler picks it up."""
    conn = get_connection()
    try:
        posting_queries.add_to_queue(
            conn, "Sample_Story", 3, "fa", "post", account_id=2,
            requires=_poster("fa").requires_mode)
        conn.commit()
        pending = posting_queries.get_pending_queue(conn, limit=20, runtime_mode="server")
        assert [r["story_name"] for r in pending] == ["Sample_Story"], (
            "a freshly scheduled FA story still would not run on the server")
    finally:
        conn.close()


# ── the desktop hand-off paths go quiet, and that is intended ────────

def test_the_pre_emptive_edit_handoff_is_now_inert():
    """`_queue_edit_for_desktop` short-circuited FA edits to the desktop
    *before* attempting them, justified by an IP block that turned out to be an
    expired session. With no platform declaring 'desktop' it never fires — the
    mechanism stays for a platform that genuinely needs it."""
    from posting import manager

    assert manager._queue_edit_for_desktop(
        "story", "X", 1, "fa", 2, _poster("fa")) is False
