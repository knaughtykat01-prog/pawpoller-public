"""Artwork registry tests — content_type discriminator isolation.

Verifies the posting registry surgery: the migration added content_type to all
three posting tables and folded it into the publications UNIQUE, a story and an
artwork with the same name coexist as distinct rows, the Stories list reads are
filtered to content_type='story', and the scheduler's pending-queue read sees
both content types.
"""

import pytest

from database import posting_queries


def test_content_type_columns_and_unique(db_conn):
    conn = db_conn
    for table in ("publications", "posting_queue", "posting_log"):
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        assert "content_type" in cols, f"{table} missing content_type"
    ddl = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='publications'"
    ).fetchone()[0]
    assert "UNIQUE(content_type" in ddl


def test_story_and_artwork_same_name_coexist(db_conn):
    """Discriminator prevents an artwork UPSERTing onto a same-named story row."""
    conn = db_conn
    sp = posting_queries.upsert_publication(
        conn, "Shared_Name", 0, "ib", account_id=1,
        content_type="story", external_id="100")
    ap = posting_queries.upsert_publication(
        conn, "Shared_Name", 0, "ib", account_id=1,
        content_type="artwork", external_id="200")
    assert sp != ap
    rows = conn.execute(
        "SELECT content_type, external_id FROM publications "
        "WHERE story_name='Shared_Name' ORDER BY content_type"
    ).fetchall()
    assert len(rows) == 2
    by_type = {r["content_type"]: r["external_id"] for r in rows}
    assert by_type == {"artwork": "200", "story": "100"}


def test_get_publications_filters_by_content_type(db_conn):
    conn = db_conn
    posting_queries.upsert_publication(
        conn, "Story_A", 0, "ib", account_id=1, content_type="story", external_id="1")
    posting_queries.upsert_publication(
        conn, "Art_B", 0, "fa", account_id=1, content_type="artwork", external_id="2")

    stories = posting_queries.get_publications(conn)  # default content_type='story'
    assert all(p["content_type"] == "story" for p in stories)
    names = {p["story_name"] for p in stories}
    assert "Story_A" in names and "Art_B" not in names

    arts = posting_queries.get_publications(conn, content_type="artwork")
    assert all(p["content_type"] == "artwork" for p in arts)
    assert {p["story_name"] for p in arts} == {"Art_B"}

    everything = posting_queries.get_publications(conn, content_type=None)
    assert {"Story_A", "Art_B"} <= {p["story_name"] for p in everything}


def test_queue_filters_by_content_type_but_scheduler_sees_both(db_conn):
    conn = db_conn
    posting_queries.add_to_queue(conn, "Story_A", 0, "ib", "post", content_type="story")
    posting_queries.add_to_queue(conn, "Art_B", 0, "fa", "post", content_type="artwork")

    assert all(q["content_type"] == "story"
               for q in posting_queries.get_queue(conn))
    assert all(q["content_type"] == "artwork"
               for q in posting_queries.get_queue(conn, content_type="artwork"))
    # The scheduler's pending read is deliberately UNfiltered.
    pend = posting_queries.get_pending_queue(conn, limit=10)
    assert {q["content_type"] for q in pend} == {"story", "artwork"}


def test_log_filters_by_content_type(db_conn):
    conn = db_conn
    posting_queries.log_posting_action(
        conn, "ib", "Story_A", 0, "post", "success", content_type="story")
    posting_queries.log_posting_action(
        conn, "fa", "Art_B", 0, "post", "success", content_type="artwork")
    assert all(e["content_type"] == "story"
               for e in posting_queries.get_posting_log(conn))
    assert all(e["content_type"] == "artwork"
               for e in posting_queries.get_posting_log(conn, content_type="artwork"))


# ── Artwork scheduling timezone normalisation (Phase 1) ──────────────
# _to_utc_sql is the single point where a picker's ISO string becomes the
# UTC 'YYYY-MM-DD HH:MM:SS' string the scheduler compares against
# datetime('now'). Getting this wrong fires every scheduled post at the
# wrong hour, so it's tested directly.

from datetime import datetime, timedelta, timezone
from fastapi import HTTPException
from routes.artwork_api import _to_utc_sql


def test_to_utc_sql_naive_treated_as_utc():
    future = (datetime.now(timezone.utc) + timedelta(days=1)).replace(microsecond=0)
    naive_iso = future.strftime("%Y-%m-%dT%H:%M:%S")  # no tz suffix
    assert _to_utc_sql(naive_iso) == future.strftime("%Y-%m-%d %H:%M:%S")


def test_to_utc_sql_converts_offset_to_utc():
    # 20:00 at +10:00 (AEST) is 10:00 UTC — the shift must be applied.
    future_date = (datetime.now(timezone.utc) + timedelta(days=2)).strftime("%Y-%m-%d")
    got = _to_utc_sql(f"{future_date}T20:00:00+10:00")
    assert got == f"{future_date} 10:00:00"


def test_to_utc_sql_rejects_past():
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")
    with pytest.raises(HTTPException) as exc:
        _to_utc_sql(past)
    assert exc.value.status_code == 400


def test_to_utc_sql_rejects_garbage():
    with pytest.raises(HTTPException) as exc:
        _to_utc_sql("not-a-date")
    assert exc.value.status_code == 400
