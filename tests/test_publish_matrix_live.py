"""The publish matrix goes stale, and loses a cell it cannot fit (3.28.0).

Two faults reported together: the matrix *"is not updating itself"* while a
story is being posted, and it *"isn't capturing all the time where things are
posted"*.

**Capture was sound.** Checked before writing any of this: zero successes in
`posting_log` with no matching `publications` row, and zero story-level
collisions. What was missing was a *refresh* — `publish_check.js` reloaded only
when the user triggered an action from the grid, so everything the scheduler
posted in the background (which is most of a drip) left the open modal stale
with nothing on screen saying so.

**The second fault was real but latent.** The grid is chapter x platform; a
publication's UNIQUE key is (story, chapter, platform, account_id,
content_type). Two accounts posting one work to one platform is two rows for
one cell, and the cell was built with a plain dict comprehension — it kept
whichever row the query returned LAST. Stories had not hit it (each posts from
a single account) but artwork already had seven such pairs live. Row order is
not a policy, so this file pins one: the live post wins, the recent one breaks
a tie, and the loser is still counted so the cell can admit it exists.
"""
from __future__ import annotations

import re
from pathlib import Path

from database import posting_queries as pq
from database.db import get_connection


def _pub(chapter, platform, account_id, status="posted", posted_at=""):
    return {
        "chapter_index": chapter,
        "platform": platform,
        "account_id": account_id,
        "status": status,
        "first_posted_at": posted_at,
    }


# ── a cell must never silently drop an account ───────────────────────

def test_two_accounts_in_one_cell_are_both_counted():
    """THE regression. One survives on screen; neither disappears."""
    best, accounts = pq.index_publications_by_cell([
        _pub(0, "da", 7), _pub(0, "da", 27),
    ])
    assert accounts[(0, "da")] == {7, 27}
    assert best[(0, "da")]["account_id"] in (7, 27)


def test_a_live_publication_outranks_a_dead_one_either_way_round():
    """Order-independence is the whole point — the old bug was that the
    answer depended on which row SQLite happened to return last."""
    live, dead = _pub(0, "ib", 1), _pub(0, "ib", 22, status="deleted")
    for rows in ([live, dead], [dead, live]):
        best, _ = pq.index_publications_by_cell(rows)
        assert best[(0, "ib")]["account_id"] == 1, \
            "a deleted publication won the cell over a live one"


def test_the_most_recent_wins_a_tie_on_status():
    best, _ = pq.index_publications_by_cell([
        _pub(1, "fa", 3, posted_at="2026-08-01T10:00:00"),
        _pub(1, "fa", 9, posted_at="2026-08-20T10:00:00"),
    ])
    assert best[(1, "fa")]["account_id"] == 9


def test_an_unknown_status_does_not_win_over_a_posted_one():
    """A status nobody has classified ranks as `failed`, not as the winner.
    Promoting the unclassifiable is how a matrix ends up showing a row it
    cannot explain."""
    best, _ = pq.index_publications_by_cell([
        _pub(0, "ws", 4, status="posted"),
        _pub(0, "ws", 5, status="something_new"),
    ])
    assert best[(0, "ws")]["account_id"] == 4


def test_every_status_the_ranking_knows_is_ordered_the_way_it_reads():
    r = pq.PUBLICATION_STATUS_RANK
    assert r["posted"] > r["draft"] > r["failed"] > r["deleted"]


def test_separate_cells_stay_separate():
    best, accounts = pq.index_publications_by_cell([
        _pub(0, "da", 7), _pub(1, "da", 7), _pub(0, "fa", 7),
    ])
    assert len(best) == 3
    assert all(len(v) == 1 for v in accounts.values())


def test_no_publications_is_empty_not_an_error():
    assert pq.index_publications_by_cell([]) == ({}, {})


# ── the poll only runs while there is something to wait for ──────────

def _seed_queue(conn, story, statuses):
    for status in statuses:
        qid = pq.add_to_queue(conn, story, 0, "da", "post", account_id=7)
        conn.execute("UPDATE posting_queue SET status = ? WHERE queue_id = ?",
                     (status, qid))
    conn.commit()


def test_active_jobs_counts_only_live_work():
    conn = get_connection()
    try:
        _seed_queue(conn, "Sample_Story",
                    ["pending", "processing", "completed", "failed", "cancelled"])
        assert pq.count_active_jobs(conn, "Sample_Story") == 2
    finally:
        conn.close()


def test_a_finished_queue_reports_zero_so_the_poll_stops():
    """The matrix polls while this is non-zero. If a failed row counted, one
    failure would leave the grid polling forever."""
    conn = get_connection()
    try:
        _seed_queue(conn, "Sample_Story", ["failed", "cancelled", "completed"])
        assert pq.count_active_jobs(conn, "Sample_Story") == 0
    finally:
        conn.close()


def test_another_story_being_posted_does_not_keep_this_one_polling():
    conn = get_connection()
    try:
        _seed_queue(conn, "Other_Story", ["pending", "pending"])
        assert pq.count_active_jobs(conn, "Sample_Story") == 0
        assert pq.count_active_jobs(conn, "Other_Story") == 2
    finally:
        conn.close()


def test_a_story_with_no_queue_rows_is_zero():
    conn = get_connection()
    try:
        assert pq.count_active_jobs(conn, "Never_Queued") == 0
    finally:
        conn.close()


def test_the_route_returns_active_jobs_and_uses_the_shared_helpers():
    """Pinning the wiring, because both halves fail silently: a response
    without `active_jobs` simply never polls, and a route that rebuilds its own
    index re-introduces the dropped-account bug next time this is edited."""
    src = Path(__file__).resolve().parent.parent / "routes" / "editor_api.py"
    body = src.read_text(encoding="utf-8", errors="replace")
    assert '"active_jobs": active_jobs' in body
    assert "posting_queries.count_active_jobs(" in body
    assert "posting_queries.index_publications_by_cell(" in body


# ── the front end actually polls, and knows when not to ──────────────

def _js(name):
    return (Path(__file__).resolve().parent.parent / "frontend" / "js" /
            name).read_text(encoding="utf-8", errors="replace")


def _code_only(src):
    """Comments are not code.

    Four tests in this repo have now passed or failed on their own prose — the
    most recent asserted a method was absent while matching the comment warning
    against using it. The lookbehind keeps protocol-relative URLs intact.
    """
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"(?<!:)//.*", "", src)


def _fn(src, header):
    """The body of one top-level function in the module, by its header."""
    body = src[src.index(header):]
    return body[:body.index("\n    }")]


def test_the_matrix_re_arms_its_poll_on_every_render():
    src = _code_only(_js("publish_check.js"))
    assert "_schedulePoll(data)" in src, \
        "nothing re-arms the poll, so it fires at most once"


def test_the_poll_is_gated_on_active_jobs():
    """An unconditional timer would hammer the endpoint for every open matrix,
    forever, including the overwhelming majority with nothing in flight."""
    fn = _fn(_code_only(_js("publish_check.js")), "function _schedulePoll")
    assert "active_jobs" in fn
    assert "return" in fn


def test_closing_the_matrix_stops_the_poll():
    """A live timer on a closed modal keeps fetching and eventually re-renders
    into a dialog the user shut."""
    src = _code_only(_js("publish_check.js"))
    assert "_clearPoll()" in _fn(src, "function close()")


def test_the_poll_waits_rather_than_clobbering_an_open_form():
    """A reload rebuilds the body, destroying the action and drip panels.
    Wiping a half-filled publish form from under the user is worse than the
    staleness this fixes, so `_busy()` defers — and it must RE-ARM, not skip,
    or the matrix stops updating the moment anyone opens a panel."""
    fn = _fn(_code_only(_js("publish_check.js")), "function _schedulePoll")
    assert "_busy()" in fn
    assert re.search(r"_busy\(\)\s*\)\s*\{\s*_schedulePoll\(data\);\s*return;", fn), \
        "a busy matrix must re-arm the poll, not drop it"


def test_busy_covers_every_panel_a_reload_would_destroy():
    fn = _fn(_code_only(_js("publish_check.js")), "function _busy()")
    for marker in ("drip-panel", "bulk-preflight-overlay", "publish-action-panel"):
        assert marker in fn, f"_busy() ignores {marker}, which a reload wipes"


def test_the_user_can_see_that_it_is_refreshing():
    """Polling that shows nothing leaves an unattended matrix looking identical
    whether it is live or stale — which was the complaint."""
    assert "stat-inflight" in _code_only(_js("publish_check.js"))
    css = (Path(__file__).resolve().parent.parent / "frontend" / "css" /
           "editor.css").read_text(encoding="utf-8", errors="replace")
    assert ".stat-inflight" in css, "the indicator has no styling and renders bare"


def test_a_shared_cell_says_so_in_the_detail_panel():
    assert "account_count" in _code_only(_js("publish_check.js"))


def test_every_helper_the_matrix_calls_is_defined():
    """The guard that has now caught three shipped-but-undefined calls
    (`API.testAccountLogin`, `this._toast`, `App.showToast`). A missing method
    is a runtime error on a path no Python test ever loads."""
    src = _code_only(_js("publish_check.js"))
    defined_api = set(re.findall(r"^\s{4}(?:async\s+)?(\w+)\s*\(", _js("api.js"), re.M))
    defined_app = set(re.findall(r"^\s{4}(?:async\s+)?(\w+)\s*\(", _js("app.js"), re.M))
    missing_api = sorted(set(re.findall(r"API\.(\w+)\s*\(", src)) - defined_api)
    missing_app = sorted(set(re.findall(r"App\.(\w+)\s*\(", src)) - defined_app)
    assert missing_api == [], f"publish_check.js calls undefined API methods: {missing_api}"
    assert missing_app == [], f"publish_check.js calls undefined App methods: {missing_app}"
