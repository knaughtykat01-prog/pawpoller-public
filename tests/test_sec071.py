"""SEC071 (spec 071 implement review Low): a stored error never names a cleaned copy's temp folder.

A poster's `error=str(e)` can name `...\\Temp\\pp-clean-ab12\\cover.jpg`, and the temp root carries the OS user
name. Every stored error keeps only the file's name.
"""
from __future__ import annotations

import os
import tempfile

import pytest

from database import posting_queries, posts_queries
from database.db import get_connection, init_db
from posting.metadata_strip import redact_temp_paths


@pytest.fixture
def temp_root(tmp_path, monkeypatch):
    root = tmp_path / "Second Fur" / "Temp"            # a space in the user name, as on many Windows machines
    root.mkdir(parents=True)
    monkeypatch.setattr(tempfile, "tempdir", str(root))
    return str(root)


def _copy(root):
    return os.path.join(root, "pp-clean-ab12cd", "cover.jpg")


def test_every_spelling_keeps_only_the_file_name(temp_root):
    path = _copy(temp_root)
    for text in (f"upload failed: {path}",
                 str(FileNotFoundError(2, "No such file or directory", path)),     # repr: doubled backslashes
                 f"upload failed: {path.replace(os.sep, '/')}"):
        out = redact_temp_paths(text)
        assert "Second Fur" not in out and "pp-clean-" not in out and "cover.jpg" in out, out
    assert redact_temp_paths("plain error") == "plain error"
    assert redact_temp_paths(None) is None
    assert redact_temp_paths("") == ""


def test_stored_errors_are_redacted(temp_root):
    init_db()
    msg = f"Could not read {_copy(temp_root)}"
    conn = get_connection()
    try:
        log_id = posting_queries.log_posting_action(conn, "ib", "Sample Piece", 0, "post", "failed",
                                                    content_type="artwork", error_message=msg)
        qid = posting_queries.add_to_queue(conn, "Sample Piece", 0, "ib", action="post", content_type="artwork")
        posting_queries.update_queue_status(conn, qid, "failed", error=msg)
        pid = posts_queries.create_post(conn, body="A sample post", now="2026-11-04T00:00:00")
        posts_queries.upsert_post_publication(conn, post_id=pid, platform="bsky", status="failed", error=msg)
        stored = [
            conn.execute("SELECT error_message FROM posting_log WHERE log_id = ?", (log_id,)).fetchone()[0],
            conn.execute("SELECT last_error FROM posting_queue WHERE queue_id = ?", (qid,)).fetchone()[0],
            conn.execute("SELECT error FROM post_publications WHERE post_id = ?", (pid,)).fetchone()[0],
        ]
    finally:
        conn.close()
    assert stored == ["Could not read cover.jpg"] * 3
