"""SEC4885: the four Lows from the 4.88.5 release review."""
import asyncio
import json

import pytest

import desktop_agent as da
from tests.test_connected_mode import FakeHttp, FakeResp


def test_rating_unreadable_is_permanent():
    """(4) RatingUnreadable never clears on retry, so it must not be retried."""
    from clients.otw_forms import raise_only_rating, RatingUnreadable
    from posting.manager import _PERMANENT_ERROR_MARKERS
    with pytest.raises(RatingUnreadable) as e:
        raise_only_rating([], "Mature", "AO3")
    assert any(m in str(e.value).lower() for m in _PERMANENT_ERROR_MARKERS)


def test_desktop_hand_over_refused_is_final_with_the_reason(tmp_path):
    """(2) A 400 from the server is shown, not queued as 'server unreachable'."""
    class Refusing(FakeHttp):
        def post(self, url, json=None, data=None, files=None, **kw):  # noqa: F811
            return FakeResp(400, text=_json.dumps({"detail": "That browser is signed in as SecondFur, not Inkwolf."}))
    _json = json
    q = da.Queue(tmp_path / "q.json")
    ag = da.Agent("https://box.example", "pp_key", http=Refusing(up=True), queue=q,
                  login_fn=lambda *a: {"th_cookie": "c"})
    out = ag.login("th", {"th_username": "Inkwolf"}, 5)
    assert out["ok"] is False and "SecondFur" in out["message"]
    assert q.pending_count() == 0


def _patch_helper(monkeypatch, saved_name, who_result=None, who_exc=None):
    from routes import signin_helper_api as sh
    seen = {}

    async def fake_who(platform, creds, stored):
        seen["stored"] = stored
        if who_exc:
            raise who_exc
        return who_result
    monkeypatch.setattr(sh, "_who", fake_who)
    monkeypatch.setattr(sh, "_account", lambda aid: {"account_id": aid, "platform": "th", "is_default": False})
    monkeypatch.setattr(sh, "_stored_name", lambda acct: saved_name)
    return seen


def test_owner_check_uses_the_saved_name_not_the_typed_one(monkeypatch):
    """(1) A browser login can't switch an existing account to another Toyhouse user."""
    from auth import browser_login as bl
    seen = _patch_helper(monkeypatch, "Inkwolf", (False, "SecondFur", ""))
    why = asyncio.run(bl.whose_session_problem("th", {"th_username": "SecondFur"}, 5))
    assert seen["stored"] == "Inkwolf" and "SecondFur" in why


def test_owner_check_falls_back_to_the_typed_name_for_a_new_account(monkeypatch):
    from auth import browser_login as bl
    seen = _patch_helper(monkeypatch, "", (True, "Penwright", ""))
    assert asyncio.run(bl.whose_session_problem("th", {"th_username": "Penwright"}, 5)) == ""
    assert seen["stored"] == "Penwright"


def test_owner_check_site_error_is_a_reason_not_a_crash(monkeypatch):
    """(3) THChallenged/THUnavailable become a refusal with a reason, not a 500."""
    from auth import browser_login as bl
    from clients.th.client import THUnavailable
    _patch_helper(monkeypatch, "Inkwolf", who_exc=THUnavailable("busy"))
    why = asyncio.run(bl.whose_session_problem("th", {"th_username": "Inkwolf"}, 5))
    assert "didn't answer" in why
