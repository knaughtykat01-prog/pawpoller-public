"""Clearing finished queue rows (3.22.0).

The operator, after 3.21.0 stopped the retry storm: *"also yes clear the 4499 failed
rows please with a button i can press"* — the button being the point. The rows
were his to delete, and deleting them once from a console would have left him
with no way to do it the next time.

What makes this safe is narrow and worth pinning: **only finished rows can ever
be reached.** `pending` and `processing` are live work — a clear that could
touch them would silently delete a scheduled post or orphan a row the scheduler
is mid-way through. `CLEARABLE_STATUSES` is a frozenset and an out-of-set status
is a hard `ValueError`, not a filtered-out no-op: silently dropping an
unexpected status would let "clear everything" report success while leaving live
rows behind, which is the more dangerous of the two failures.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from database import posting_queries as pq
from database.db import get_connection


@pytest.fixture()
def client():
    from dashboard import app
    return TestClient(app)


def _seed(conn):
    """One row in every status the queue can hold."""
    ids = {}
    for status in ("pending", "processing", "failed", "cancelled", "completed"):
        qid = pq.add_to_queue(conn, f"Story_{status}", 0, "da", "post", account_id=7)
        conn.execute("UPDATE posting_queue SET status = ? WHERE queue_id = ?",
                     (status, qid))
        ids[status] = qid
    conn.commit()
    return ids


def _statuses(conn):
    return {r[0] for r in conn.execute("SELECT status FROM posting_queue")}


# ── the safety property ──────────────────────────────────────────────

def test_live_work_is_never_deleted():
    """THE test. Scheduled and in-flight rows must survive any clear."""
    conn = get_connection()
    try:
        _seed(conn)
        pq.clear_queue_rows(conn, ["failed", "cancelled", "completed"])
        assert _statuses(conn) == {"pending", "processing"}
    finally:
        conn.close()


def test_a_status_outside_the_clearable_set_is_refused():
    conn = get_connection()
    try:
        _seed(conn)
        with pytest.raises(ValueError, match="refusing to clear"):
            pq.clear_queue_rows(conn, ["failed", "pending"])
        # and nothing was deleted on the way to the refusal
        assert len(_statuses(conn)) == 5
    finally:
        conn.close()


def test_the_refusal_names_what_it_would_have_allowed():
    conn = get_connection()
    try:
        with pytest.raises(ValueError) as e:
            pq.clear_queue_rows(conn, ["processing"])
        assert "processing" in str(e.value) and "failed" in str(e.value)
    finally:
        conn.close()


def test_pending_cannot_be_reached_through_the_endpoint_either(client):
    conn = get_connection()
    try:
        _seed(conn)
    finally:
        conn.close()
    r = client.post("/api/posting/queue/clear", json={"statuses": ["pending"]})
    assert r.status_code == 400
    conn = get_connection()
    try:
        assert len(_statuses(conn)) == 5
    finally:
        conn.close()


# ── ordinary behaviour ───────────────────────────────────────────────

def test_it_clears_only_failed_by_default(client):
    conn = get_connection()
    try:
        _seed(conn)
    finally:
        conn.close()
    body = client.post("/api/posting/queue/clear", json={}).json()
    assert body["cleared"] == 1
    conn = get_connection()
    try:
        assert _statuses(conn) == {"pending", "processing", "cancelled", "completed"}
    finally:
        conn.close()


def test_clearing_an_empty_queue_is_zero_not_an_error():
    conn = get_connection()
    try:
        assert pq.clear_queue_rows(conn, ["failed"]) == 0
        assert pq.clear_queue_rows(conn, []) == 0
    finally:
        conn.close()


def test_the_count_endpoint_reports_what_the_clear_would_delete(client):
    conn = get_connection()
    try:
        _seed(conn)
        for _ in range(4):
            qid = pq.add_to_queue(conn, "Noisy", 0, "ao3", "post", account_id=1)
            conn.execute("UPDATE posting_queue SET status='failed' WHERE queue_id=?", (qid,))
        conn.commit()
    finally:
        conn.close()

    counts = client.get("/api/posting/queue/clearable").json()
    assert counts["by_status"]["failed"] == 5
    assert counts["total"] == 7          # 5 failed + 1 cancelled + 1 completed
    assert counts["by_platform"]["ao3"] == 4

    cleared = client.post("/api/posting/queue/clear",
                          json={"statuses": ["failed", "cancelled", "completed"]}).json()
    assert cleared["cleared"] == counts["total"], \
        "the number on the button must be the number that gets deleted"


def test_the_clearable_route_is_not_swallowed_by_the_id_route(client):
    """`/queue/clearable` has to be declared before `/queue/{queue_id}` —
    FastAPI matches in definition order, and "clearable" parsed as an int is a
    422. Pinning it because the fix is invisible: reordering the file breaks
    this with no other symptom."""
    r = client.get("/api/posting/queue/clearable")
    assert r.status_code == 200
    assert "by_status" in r.json()


# ── the front door exists and calls things that exist ────────────────

def _js(name):
    from pathlib import Path
    return (Path(__file__).resolve().parent.parent / "frontend" / "js" /
            name).read_text(encoding="utf-8", errors="replace")


def test_the_queue_page_has_the_button():
    src = _js("posting.js")
    assert "data-q-clear" in src
    assert "_wireClearFinished" in src


def test_the_button_confirms_before_deleting():
    """It cannot be undone, so it must not be one click."""
    src = _js("posting.js")
    handler = src[src.index("_wireClearFinished"):]
    assert "confirm(" in handler[:2000]


def test_every_helper_the_queue_page_calls_is_defined():
    """The guard that earned its place twice in one day.

    `accounts.js` shipped calling `API.testAccountLogin(...)` with no definition
    (3.20.1), and the Sync panel shipped calling `this._toast(...)` the same way
    (3.18.1). Writing this module I reached for `App.showToast(...)`, which also
    does not exist — caught here rather than in the owner's browser, because a
    missing method is a runtime error on a path a Python suite never loads.
    """
    import re

    def _code_only(src):
        """Comments are not call sites.

        The first run of this test failed on `App.showToast` — which appears in
        posting.js exactly once, inside the comment warning against using it.
        Scanning prose for code is how three earlier tests in this repo passed
        or failed on their own docstrings. The `(?<!:)` keeps `https://` intact.
        """
        src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
        # No re.S here, so `.` stops at the newline — one line comment each.
        return re.sub(r"(?<!:)//.*", "", src)

    posting = _code_only(_js("posting.js"))
    defined_api = set(re.findall(r"^\s{4}(?:async\s+)?(\w+)\s*\(", _js("api.js"), re.M))
    defined_app = set(re.findall(r"^\s{4}(?:async\s+)?(\w+)\s*\(", _js("app.js"), re.M))

    missing_api = sorted(set(re.findall(r"API\.(\w+)\s*\(", posting)) - defined_api)
    missing_app = sorted(set(re.findall(r"App\.(\w+)\s*\(", posting)) - defined_app)
    assert missing_api == [], f"posting.js calls undefined API methods: {missing_api}"
    assert missing_app == [], f"posting.js calls undefined App methods: {missing_app}"
