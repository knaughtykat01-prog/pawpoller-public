"""Setup stays open (056) in a real browser: the gate, the strip, the return from a site's page,
Finish later and the resume walk. Uses test_frontend_runs' app and browser (seeded, offline)."""
from __future__ import annotations

import config
import pytest  # noqa: E402

from tests.test_frontend_runs import _offline, _tooling_missing, _Watched, app_url, browser  # noqa: F401  (fixtures)

# The release image has no Playwright: skip there, like test_frontend_runs.
pytestmark = pytest.mark.skipif(bool(_tooling_missing()), reason=_tooling_missing() or "ok")


def _unfinished(step: str, **extra):
    config.save_settings({"setup_complete": False,
                          "setup_progress": {"step": step, "skipped": [], "return_to": None, **extra}})


def _hash(tab) -> str:
    return tab.page.evaluate("location.hash")


def test_setup_stays_open(browser, app_url):  # noqa: F811
    _unfinished("platforms")
    tab = _Watched(browser, app_url, {"width": 390, "height": 844})
    try:
        page = tab.page
        stacks = []
        page.on("pageerror", lambda e: stacks.append(e.stack))

        # The gate: a page setup doesn't need goes back to the wizard, with the reason.
        page.evaluate("location.hash = '#/library'")
        page.wait_for_function("() => location.hash === '#/setup'", timeout=5000)
        page.wait_for_selector(".setup-reason", timeout=5000)
        assert "Library" in page.inner_text(".setup-reason")

        # The strip: on a page setup does need, one line and a way back.
        page.evaluate("location.hash = '#/accounts'")
        page.wait_for_selector("#setup-strip", timeout=5000)
        assert "Setup isn't finished" in page.inner_text("#setup-strip")
        assert _hash(tab) == "#/accounts"
        page.wait_for_load_state("networkidle")       # let the page finish drawing before leaving it

        # The return: Connect from the wizard, the site reads connected (dummy key), back to setup.
        page.evaluate("location.hash = '#/setup'")
        page.wait_for_selector('.setup-platform-connect[data-site="ws"]', timeout=8000)
        page.click('.setup-platform-connect[data-site="ws"]')
        page.wait_for_function("() => location.hash.startsWith('#/settings/platforms/ws')", timeout=5000)
        page.wait_for_function("() => location.hash === '#/setup'", timeout=8000)
        assert config.get_settings()["setup_progress"]["return_to"] is None
        page.wait_for_selector(".setup-platform-guide[data-guide]", timeout=8000)   # the Guide button (US4)

        # Finish later: listed, confirmed, complete with the rest kept for Settings.
        page.once("dialog", lambda d: d.accept())
        page.click("#setup-later")
        page.wait_for_function("() => !location.hash.startsWith('#/setup')", timeout=8000)
        page.wait_for_load_state("networkidle")
        tab.errors.clear()      # the reload cuts off the old page's requests; only the new page counts
        s = config.get_settings()
        assert s["setup_complete"] is True
        skipped = s["setup_progress"]["skipped"]
        assert "platforms" in skipped and "age" not in skipped
        assert page.query_selector("#setup-strip") is None

        # Resume: Settings' button walks only what was left.
        page.evaluate("location.hash = '#/setup?resume=1'")
        page.wait_for_selector(".setup-wizard", timeout=8000)
        dots = page.query_selector_all(".setup-step-dot")
        assert len(dots) == len([x for x in skipped if x]) + 1          # the left steps + Finish
        assert not tab.errors, (tab.errors, stacks)
    finally:
        tab.close()
        config.save_settings({"setup_complete": True})
