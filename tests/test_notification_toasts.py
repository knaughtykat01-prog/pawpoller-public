"""The bell's toasts (4.42.1): no replay storms, no red "no changes" for a throttled poll.

A tester's screen filled with sticky red "no changes" toasts. Two causes:
the feed is a sliding window, so older rows scrolling into it looked unseen and
all toasted at once; and a throttled ('partial') poll row toasted as an error
reading "no changes" — while a separate 'throttle' warning already covers it.
The JS is run under node against a fake DOM/API so the real logic is tested.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from routes.api import _format_poll_summary

JS = (Path(__file__).resolve().parent.parent / "frontend" / "js" / "notifications_center.js").read_text(encoding="utf-8")


def test_throttled_poll_is_not_called_no_changes():
    assert _format_poll_summary({"status": "partial"}) == "throttled — some data may be missing"
    assert _format_poll_summary({"status": "partial", "new_faves_found": 2}) == "throttled, +2 faves"
    assert _format_poll_summary({"status": "success"}) == "no posts found"


def _run(feeds: list[list[dict]]) -> list[list[str]]:
    """Feed successive /api/notifications answers through poll(); return the toasts per poll."""
    harness = """
    const feeds = %s; let i = 0; const toasts = [];
    const el = () => ({ appendChild(){}, addEventListener(){}, setAttribute(){}, classList: { add(){}, remove(){}, toggle(){} },
                        querySelector: () => el(), querySelectorAll: () => [], style: {}, hidden: true, innerHTML: '', textContent: '' });
    global.document = { readyState: 'complete', getElementById: () => null, createElement: el,
                        body: el(), addEventListener(){}, querySelector: () => el() };
    global.window = { toast: { error: (m) => toasts.push('E:' + m), warn: (m) => toasts.push('W:' + m) } };
    global.API = { getNotifications: async () => ({ items: feeds[i++], unread: 0 }) };
    global.setInterval = () => 1;
    %s
    (async () => {
      const out = [];
      for (let n = 0; n < feeds.length; n++) { toasts.length = 0; await window.NotificationCenter.poll(); out.push([...toasts]); }
      console.log(JSON.stringify(out));
    })();
    """ % (json.dumps(feeds), JS)
    r = subprocess.run(["node", "-e", harness], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


def _ev(ts, status="error", summary="boom", kind="poll", platform="tw"):
    return {"timestamp": ts, "status": status, "summary": summary, "kind": kind, "platform": platform}


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_old_rows_scrolling_into_the_window_do_not_toast():
    seed = [_ev("2026-09-28 10:00:00", summary="A")]
    # Next poll: a new failure AND three OLDER failures that slid into the window.
    later = [_ev("2026-09-28 11:00:00", summary="NEW"), *seed,
             *[_ev(f"2026-09-27 0{n}:00:00", summary=f"old{n}") for n in range(3)]]
    out = _run([seed, later])
    assert out == [[], ["E:NEW"]]


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_a_throttled_poll_row_does_not_toast_red_but_its_warning_does():
    seed = [_ev("2026-09-28 10:00:00", status="success", summary="no changes")]
    nxt = [_ev("2026-09-28 11:00:00", status="partial", summary="throttled — some data may be missing"),
           _ev("2026-09-28 11:00:00", status="warn", kind="throttle", summary="X: last poll was throttled"), *seed]
    assert _run([seed, nxt]) == [[], ["W:X: last poll was throttled"]]


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_live_alerts_still_toast_even_when_older_than_the_newest_row():
    seed = [_ev("2026-09-28 11:00:00", status="success", summary="+1 fave")]
    nxt = [*seed, _ev("2026-09-28T10:30:00+00:00", status="error", kind="session", summary="FA session expired")]
    assert _run([seed, nxt]) == [[], ["E:FA session expired"]]
