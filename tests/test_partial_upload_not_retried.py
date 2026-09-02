"""A post whose upload succeeded must not be retried (3.9.9).

FurryNetwork uploads the bytes first (the submission lands as a **draft**) and
then PATCHes the metadata to title/tag/rate it and make it public. When the
PATCH failed, prod scheduled a retry:

    Retry: Blows_a_kiss ch0 on fn queued for 2026-08-19 06:06:02
    (attempt 1, error: uploaded (id 1896467) but metadata PATCH failed (HTTP 422))

A retry re-runs the whole post from scratch. It cannot repair submission
1896467, and it leaves *another* draft on FurryNetwork every time — so the
retry loop can only multiply orphans, never succeed.

The 422 body was also thrown away, leaving "HTTP 422" as the entire diagnosis.
That is the same failure of reporting as the FN auth wall reading as "HTTP 422"
until `message` was surfaced.
"""
from __future__ import annotations

from posting.manager import _schedule_retry


def test_a_partial_upload_is_not_retried(monkeypatch, caplog):
    """The whole point: retrying makes a second copy, never a fix."""
    scheduled = _schedule_retry(
        "Blows_a_kiss", 0, "fn", "post",
        "already uploaded (id 1896467) but metadata PATCH failed (HTTP 422): {...}",
        account_id=28)
    assert scheduled is False


def test_the_reason_says_what_to_do_about_it(caplog):
    import logging

    with caplog.at_level(logging.WARNING):
        _schedule_retry("Blows_a_kiss", 0, "fn", "post",
                        "already uploaded (id 1896467) but metadata PATCH failed (HTTP 422)",
                        account_id=28)
    text = caplog.text
    assert "second copy" in text
    assert "delete the existing submission" in text
    # The platform's id has to survive into the log, or the operator cannot find
    # the orphan it is telling them about.
    assert "1896467" in text


def test_an_unconfigured_platform_is_still_not_retried():
    """The pre-existing permanent-error case must keep working."""
    assert _schedule_retry("X", 0, "da", "post",
                           "DeviantArt OAuth not configured. Set da_client_id") is False


def test_an_ordinary_failure_is_still_retried():
    """Only the permanent classes stop. A transient error must still back off
    and try again, or this fix would break retries generally.

    Runs against the real (per-test, temp) database rather than a stub
    connection: since 3.21.0 the attempt number is counted from the queue, so a
    stub that only implements close() no longer models anything real.
    """
    from database.db import get_connection

    assert _schedule_retry("X", 0, "sf", "post", "HTTP 503 upstream hiccup") is True

    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT priority FROM posting_queue WHERE platform = 'sf'").fetchall()
    finally:
        conn.close()
    assert len(rows) == 1 and rows[0][0] == -1


def test_the_client_and_the_classifier_agree_on_the_phrase():
    """The marker is a string in two files. If the client stops emitting it, the
    retry guard silently stops firing and the orphans come back."""
    import inspect

    from clients.fn import client as fn_client
    from posting import manager

    assert "already uploaded" in inspect.getsource(fn_client.FnClient.upload_artwork)
    assert "already uploaded" in inspect.getsource(manager._schedule_retry)


def test_the_client_carries_the_platforms_own_error_text():
    """A bare status code is not a diagnosis — 422 is precisely the status that
    arrives with a body naming the offending field."""
    import inspect

    from clients.fn import client as fn_client

    src = inspect.getsource(fn_client.FnClient.upload_artwork)
    assert "pr.text" in src, "the PATCH response body must reach the error"
