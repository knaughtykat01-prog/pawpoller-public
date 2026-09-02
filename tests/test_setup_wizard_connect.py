"""The setup wizard's Connect button must lead somewhere that can connect.

A tester on the 4.0.1 desktop build reported that pressing Connect in the
first-run wizard "just opens a tab on my browser and asks me to log in, but
then does nothing", on every platform they tried. It was not a regression and
not intermittent: the card's only action was

    <a href="${p.url}" target="_blank">Connect</a>

where `p.url` is the platform's own login page. It opened the website, captured
nothing, and stored nothing, so the card could never move off "Not connected"
and pressing it again simply reopened the tab.

What made it convincing is that the card was RIGHT about state — `authStatus`
is real, so a platform connected elsewhere correctly showed "Connected" — and
wrong only about the action.

Connect now deep-links to that platform's credential form at
``#/settings/platforms/<key>``. These tests hold the two halves of that link
together: the wizard must emit an in-app href, and every key it emits must
match an accordion that actually exists.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
APP_JS = REPO / "frontend" / "js" / "app.js"

pytestmark = pytest.mark.skipif(not APP_JS.is_file(), reason="frontend/js/app.js not present")


@pytest.fixture(scope="module")
def app_js() -> str:
    return APP_JS.read_text(encoding="utf-8")


def _wizard_platform_keys(src: str) -> list[str]:
    """The `key:` values from renderSetupWizard's platform list."""
    start = src.index("renderSetupWizard()")
    block = src[start:src.index("];", start)]
    return re.findall(r"\{\s*key:\s*'([\w-]+)'", block)


def _accordion_codes(src: str) -> set[str]:
    return set(re.findall(r'settings-accordion"\s+data-platform="([\w-]+)"', src))


def test_the_wizard_lists_platforms_at_all(app_js: str):
    keys = _wizard_platform_keys(app_js)
    assert len(keys) >= 15, f"wizard platform list looks truncated: {keys}"


def test_connect_does_not_just_open_the_platform_website(app_js: str):
    """THE regression test.

    The card may still carry a secondary link to the platform's site — several
    platforms need you to go there to generate an API key — but the PRIMARY
    action must be in-app. Asserting on the class keeps this honest: a bare
    `target="_blank"` primary action is exactly the bug.
    """
    start = app_js.index("setup-platform-card")
    card = app_js[start:start + 1600]

    connect = re.search(r'<a href="([^"]+)"[^>]*class="[^"]*setup-platform-connect', card)
    assert connect, (
        "the wizard card has no primary connect link with class "
        "'setup-platform-connect' — if it was replaced, make sure the "
        "replacement still points somewhere that can actually store credentials"
    )
    href = connect.group(1)
    assert href.startswith("#/settings/platforms/"), (
        f"Connect must deep-link to the in-app credential form, got {href!r}. "
        "Linking to the platform's own website captures nothing, which is the "
        "bug this test exists for."
    )


def test_every_wizard_platform_has_a_credential_form_to_link_to(app_js: str):
    """A key with no matching accordion is a dead link — the same user-visible
    failure as the original bug, arrived at from the other direction."""
    keys = _wizard_platform_keys(app_js)
    codes = _accordion_codes(app_js)
    missing = sorted(k for k in keys if k not in codes)
    assert not missing, (
        "these wizard platforms deep-link to a credential form that does not "
        f"exist, so Connect would land on the tab and do nothing: {missing}. "
        "Either add a data-platform accordion in Settings -> Platforms, or "
        "drop the platform from the wizard list."
    )


def test_the_deep_link_handler_exists(app_js: str):
    """The href is inert without something to open the accordion it names."""
    assert "_focusPlatformFromHash" in app_js, "the deep-link handler is gone"
    assert "settings/platforms" in app_js
    assert app_js.count("_focusPlatformFromHash") >= 2, (
        "handler is defined but never called — the link would land on the "
        "Platforms tab with all nineteen accordions still collapsed"
    )


def test_a_user_sent_from_the_wizard_can_get_back(app_js: str):
    """Otherwise the deep link trades one dead end for another."""
    assert "setup-return-banner" in app_js, (
        "no return-to-setup affordance on the Platforms tab; a user sent there "
        "mid-wizard has no visible way back to finish setup"
    )


def test_the_wizard_remembers_its_step_across_the_round_trip(app_js: str):
    """Verified in a real browser, then pinned here.

    Connect deep-links out of the wizard into Settings, and renderSetupWizard()
    re-runs on the way back — so `currentStep` was reinitialised to 'welcome'
    every time. A user connecting several platforms walked Welcome → mode →
    archive → platforms again for EACH one. With seventeen platforms on that
    step, the fix for the dead button would have shipped a new annoyance.

    Static checks, because the behaviour they guard was confirmed live.
    """
    start = app_js.index("renderSetupWizard()")
    block = app_js[start:start + 30000]

    assert "pp.setup.progress" in block, "wizard progress is no longer persisted"
    assert "sessionStorage" in block, (
        "progress must live in sessionStorage — it should not outlive the tab, "
        "and must never be restored into a later setup run"
    )
    assert "!== 'done'" in block, (
        "a restored step must exclude 'done'; returning straight to the final "
        "screen would skip the setup it claims to have finished"
    )
    assert "removeItem" in app_js[start:start + 60000], (
        "the saved progress is never cleared on completion"
    )


def test_the_restored_step_is_validated_against_the_current_path(app_js: str):
    """The mode picker changes which steps exist (a paired desktop skips
    archive and platforms entirely), so a step saved under one mode may not
    exist under another. Restoring it blindly would strand the wizard on a step
    its own navigation cannot reach."""
    start = app_js.index("renderSetupWizard()")
    block = app_js[start:start + 30000]
    assert "stepOrder().includes(" in block, (
        "restored step is not validated against the current stepOrder()"
    )
