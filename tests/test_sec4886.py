"""SEC4886: the two Lows from the 4.88.6 release review."""
import asyncio

from tests.test_sec4885 import _patch_helper


def test_a_typed_name_that_differs_from_the_saved_one_is_not_written(monkeypatch):
    """(1) The check passed against the SAVED name, so that is the name written."""
    from auth import browser_login as bl
    _patch_helper(monkeypatch, "Inkwolf", (True, "Inkwolf", ""))
    creds = {"th_cookie": "c", "th_username": "SecondFur"}
    assert asyncio.run(bl.whose_session_problem("th", creds, 5)) == ""
    assert creds["th_username"] == "Inkwolf"


def test_a_new_account_keeps_its_typed_name(monkeypatch):
    from auth import browser_login as bl
    _patch_helper(monkeypatch, "", (True, "Penwright", ""))
    creds = {"th_cookie": "c", "th_username": "Penwright"}
    assert asyncio.run(bl.whose_session_problem("th", creds, 5)) == ""
    assert creds["th_username"] == "Penwright"


def test_finding_the_default_account_never_creates_one(monkeypatch):
    """(2) No account_id: the default is looked up with create=False."""
    from auth import browser_login as bl
    from routes import signin_helper_api as sh
    _patch_helper(monkeypatch, "", (False, "SecondFur", ""))
    calls = []
    monkeypatch.setattr(sh, "_default_account_id", lambda platform, create=True: calls.append(create))
    asyncio.run(bl.whose_session_problem("th", {"th_username": "Penwright"}, None))
    assert calls == [False]
