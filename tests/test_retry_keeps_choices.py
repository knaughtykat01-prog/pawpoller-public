"""RETRYKEEPS (4.88.9): a retry posts the same way the first try did.

A failed post is queued again by ``_schedule_retry`` (and, on the server, a desktop-only site's
post is queued for the desktop app). Those rows used to drop two choices the person made:

- the story's public/draft choice: a post sent as a draft came back from a retry as a live post;
- the persona the schedule was made for: the scheduler re-checks the account against it when the
  row fires, and a row with no persona skips that check.
"""
from __future__ import annotations

import ast
import inspect
import textwrap

from database.db import get_connection
from posting import manager
from posting.manager import _schedule_retry


def _row(platform):
    conn = get_connection()
    try:
        return dict(conn.execute(
            "SELECT draft, persona_id FROM posting_queue WHERE platform = ?", (platform,)).fetchone())
    finally:
        conn.close()


def test_retry_row_keeps_draft_and_persona():
    assert _schedule_retry("Sample_Story", 1, "sf", "post", "HTTP 503", persona_id=4, draft=True)
    assert _row("sf") == {"draft": 1, "persona_id": 4}


def test_retry_row_keeps_a_public_choice():
    """draft=False is a choice too (post publicly), not the same as no choice."""
    assert _schedule_retry("Sample_Story", 1, "ib", "post", "HTTP 503", draft=False)
    assert _row("ib") == {"draft": 0, "persona_id": None}


def test_no_choice_stays_no_choice():
    assert _schedule_retry("Sample_Story", 1, "da", "post", "HTTP 503")
    assert _row("da") == {"draft": None, "persona_id": None}


def _calls(fn, name):
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    return [c for c in ast.walk(tree) if isinstance(c, ast.Call)
            and getattr(c.func, "attr", getattr(c.func, "id", None)) == name]


def test_every_requeue_in_the_post_paths_carries_the_persona():
    """Both kinds of requeue, in both post paths: a new one that forgets the persona fails here."""
    for fn in (manager._post_story, manager._post_artwork):
        calls = _calls(fn, "_schedule_retry") + _calls(fn, "add_to_queue")
        assert len(calls) >= 2, fn.__name__
        for c in calls:
            assert "persona_id" in {k.arg for k in c.keywords}, f"{fn.__name__} line {c.lineno}"


def test_story_requeues_carry_the_draft_choice():
    calls = _calls(manager._post_story, "_schedule_retry") + _calls(manager._post_story, "add_to_queue")
    for c in calls:
        assert "draft" in {k.arg for k in c.keywords}, f"_post_story line {c.lineno}"
