"""A FurAffinity edit on the server is queued, not attempted (3.9.6).

FA blocks the datacenter IP outright — `/controls/` pages return an empty shell
even with valid cookies — so a server-side edit is not unlikely to work, it is
guaranteed not to. Prod, 2026-08-19:

    FA edit failed for 37056222: FA: Could not find changeinfo form on edit page

That is the empty shell, reported as though FA had changed its page shape.

Both edit paths already queued for desktop, but only as failure *recovery* —
`update_story` after the doomed request and traceback, and `update_artwork` not
at all, so an FA artwork edit failed and stayed failed with nothing queued to
retry it anywhere it could work.
"""
from __future__ import annotations

import pytest

from posting import manager


class _Poster:
    requires_mode = "desktop"
    supports_edit = True


class _AnyPoster:
    requires_mode = "any"
    supports_edit = True


@pytest.fixture
def queued(monkeypatch):
    """Capture add_to_queue instead of touching a database."""
    calls = []

    def _add(conn, name, ch, plat, action, **kw):
        calls.append({"name": name, "chapter": ch, "platform": plat,
                      "action": action, **kw})
        return 1

    monkeypatch.setattr(manager.posting_queries, "add_to_queue", _add)
    monkeypatch.setattr(manager, "get_connection", lambda: _FakeConn())
    return calls


class _FakeConn:
    def close(self):
        pass


def _set_mode(monkeypatch, mode):
    import posting.scheduler as sched
    monkeypatch.setattr(sched, "_runtime_mode", mode, raising=False)


def test_a_desktop_only_edit_on_the_server_is_queued(queued, monkeypatch):
    _set_mode(monkeypatch, "server")
    assert manager._queue_edit_for_desktop("story", "Chosen", 1, "fa", 2, _Poster()) is True
    assert len(queued) == 1
    assert queued[0]["platform"] == "fa"
    assert queued[0]["requires"] == "desktop"
    assert queued[0]["action"] == "update"


def test_the_content_type_is_carried_so_the_scheduler_routes_it_back(queued, monkeypatch):
    """The scheduler branches on the queued row's content_type to decide whether
    a retry goes to post_story or post_artwork."""
    _set_mode(monkeypatch, "server")
    manager._queue_edit_for_desktop("artwork", "Rear_View", 0, "fa", 2, _Poster())
    assert queued[0]["content_type"] == "artwork"
    assert queued[0]["chapter"] == 0


def test_on_the_desktop_the_edit_is_attempted_normally(queued, monkeypatch):
    """The desktop is where it CAN work — queueing there would loop forever."""
    _set_mode(monkeypatch, "desktop")
    assert manager._queue_edit_for_desktop("story", "Chosen", 1, "fa", 2, _Poster()) is False
    assert queued == []


def test_a_platform_that_works_from_the_server_is_untouched(queued, monkeypatch):
    """FA is the only platform declaring requires_mode='desktop'. Everything
    else must still edit in place, on either box."""
    _set_mode(monkeypatch, "server")
    assert manager._queue_edit_for_desktop("story", "Chosen", 1, "sf", 2, _AnyPoster()) is False
    assert queued == []


def test_both_edit_paths_use_the_guard():
    """update_artwork had no desktop handling at all. Pin that both call it, so
    the artwork path cannot silently lose it again."""
    import inspect

    story_src = inspect.getsource(manager.update_story)
    artwork_src = inspect.getsource(manager.update_artwork)
    assert "_queue_edit_for_desktop" in story_src
    assert "_queue_edit_for_desktop" in artwork_src


def test_the_guard_runs_before_the_edit_is_attempted():
    """The point is skipping a request that cannot succeed — checking after
    `poster.edit` would be the old behaviour with extra steps."""
    import inspect

    for fn in (manager.update_story, manager.update_artwork):
        src = inspect.getsource(fn)
        guard = src.index("_queue_edit_for_desktop")
        attempt = src.index("await poster.edit(")
        assert guard < attempt, f"{fn.__name__} checks too late"
