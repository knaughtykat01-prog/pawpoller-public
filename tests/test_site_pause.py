"""The server operator can pause a site on every seat (spec 042).

``homebox.sh pause <site>`` writes a list into a folder every seat mounts read-only. The per-site wrapper that already
takes turns reads it first, so a paused site is skipped before any request, scheduled or Poll Now.
"""
import asyncio

import pytest

from polling import last_check, site_turns
from polling.site_turns import SitePaused, TurnTimeout, operator_paused, site_turn


@pytest.fixture
def opdir(tmp_path, monkeypatch):
    monkeypatch.setenv("PAWPOLLER_OPERATOR_DIR", str(tmp_path))
    monkeypatch.delenv("PAWPOLLER_TURN_DIR", raising=False)
    monkeypatch.setattr(last_check, "_skips", {})
    counted = []
    monkeypatch.setattr("database.db.get_connection", lambda: counted.append(1))
    return tmp_path, counted


def _cycle(code, calls):
    @site_turn(code)
    async def run():
        calls.append(code)
        return "polled"
    return run


def test_a_paused_site_is_skipped_before_it_runs(opdir):
    d, counted = opdir
    (d / "paused").write_text("fa\n")
    calls = []
    with pytest.raises(SitePaused) as exc:
        asyncio.run(_cycle("fa", calls)())
    assert calls == [] and isinstance(exc.value, TurnTimeout)       # every caller already skips a TurnTimeout
    assert "server operator" in str(exc.value)
    assert asyncio.run(_cycle("ao3", calls)()) == "polled"            # other sites carry on


def test_the_list_is_read_fresh_so_resume_needs_no_restart(opdir):
    d, _ = opdir
    calls = []
    run = _cycle("fa", calls)
    (d / "paused").write_text("fa\n")
    with pytest.raises(SitePaused):
        asyncio.run(run())
    (d / "paused").write_text("")
    assert asyncio.run(run()) == "polled"


def test_junk_lines_are_ignored(opdir):
    d, _ = opdir
    (d / "paused").write_text("fa\n  ao3  \nFA\n../etc\n" + "x" * 40 + "\n\n")
    assert operator_paused() == {"fa", "ao3"}


def test_off_the_box_nothing_is_paused(monkeypatch, tmp_path):
    monkeypatch.delenv("PAWPOLLER_OPERATOR_DIR", raising=False)
    assert operator_paused() == set()
    monkeypatch.setenv("PAWPOLLER_OPERATOR_DIR", str(tmp_path / "missing"))
    assert operator_paused() == set()


def test_a_folder_in_its_place_is_not_read(opdir):
    d, _ = opdir
    (d / "paused").mkdir()
    assert operator_paused() == set()


def test_the_owner_sees_why_and_rate_watch_does_not_count_it(opdir):
    d, counted = opdir
    (d / "paused").write_text("fa\n")
    with pytest.raises(SitePaused):
        asyncio.run(_cycle("fa", [])())
    assert last_check.skips()["fa"][1] == "paused by the server operator"
    assert counted == []                                              # no turn_skips row


def test_the_multi_account_loop_skips_it_like_a_turn_timeout():
    from polling import multi_account
    src = __import__("inspect").getsource(multi_account)
    assert "except TurnTimeout" in src and issubclass(site_turns.SitePaused, TurnTimeout)
