"""Complete analytics CSV export (gap G5).

The Analytics page already exports the two summary tables client-side; this
endpoint is the full dataset — one row per publication (work × platform) with
its stats. Covers the header, the empty case, and that a seeded publication
appears with its fields in the right columns.
"""
import csv
import io

from fastapi import FastAPI
from fastapi.testclient import TestClient

from database.db import get_connection
from database import posting_queries
from routes.submissions_api import works_router


def _client():
    app = FastAPI()
    app.include_router(works_router)
    return TestClient(app)


def _rows(text):
    return list(csv.DictReader(io.StringIO(text)))


def test_export_empty_db_is_header_only():
    r = _client().get("/api/works/export.csv")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    reader = list(csv.reader(io.StringIO(r.text)))
    assert reader[0][:4] == ["content_type", "work", "title", "chapter"]
    assert len(reader) == 1                       # header only, no crash on empty


def test_export_includes_seeded_publication():
    conn = get_connection()
    try:
        posting_queries.upsert_publication(
            conn, "My Work", 0, "fa",
            content_type="story", external_id="12345",
            external_url="https://furaffinity.net/view/12345",
            title_used="My Work", rating_used="general", word_count=4200,
            status="posted",
        )
        conn.commit()
    finally:
        conn.close()

    rows = _rows(_client().get("/api/works/export.csv").text)
    assert len(rows) == 1
    row = rows[0]
    assert row["work"] == "My Work"
    assert row["platform"] == "fa"
    assert row["url"] == "https://furaffinity.net/view/12345"
    assert row["rating"] == "general"
    assert row["words"] == "4200"
