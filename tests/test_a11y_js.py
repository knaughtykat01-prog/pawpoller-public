"""frontend/js/a11y.js in a real browser (spec 012): dialog focus and screen-reader announcements.

Headless Chromium via Playwright (dev-only). Skips, with the reason, where it isn't installed.
"""
from __future__ import annotations

from pathlib import Path

import pytest

JS = Path(__file__).resolve().parent.parent / "frontend" / "js" / "a11y.js"

PAGE = """<!doctype html><html lang="en"><body>
<button id="opener">Open</button>
<script>
  document.getElementById('opener').addEventListener('click', () => {
    const ov = document.createElement('div');
    ov.className = 'modal-overlay open';
    ov.innerHTML = '<div class="modal-panel"><h3>Publish?</h3>'
      + '<button class="modal-close" aria-label="Close">×</button>'
      + '<input id="f1" aria-label="Title"><button id="cancel">Cancel</button><button id="ok">OK</button></div>';
    ov.querySelector('#cancel').addEventListener('click', () => ov.remove());
    ov.querySelector('.modal-close').addEventListener('click', () => ov.remove());
    document.body.appendChild(ov);
  });
</script></body></html>"""


@pytest.fixture(scope="module")
def page():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("Playwright for Python is not installed")
    with sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as e:  # noqa: BLE001
            pytest.skip(f"Chromium is not available: {e}")
        pg = b.new_page()
        yield pg
        b.close()


def _fresh(page):
    page.set_content(PAGE)
    page.add_script_tag(content=JS.read_text(encoding="utf-8"))
    page.focus("#opener")
    page.keyboard.press("Enter")
    page.wait_for_timeout(80)


def active(page) -> str:
    return page.evaluate("document.activeElement.id || document.activeElement.className")


def test_opening_a_dialog_moves_focus_in_and_names_it(page):
    _fresh(page)
    assert active(page) == "f1"                                   # the first real control, not the ×
    ov = page.locator(".modal-overlay")
    assert ov.get_attribute("role") == "dialog" and ov.get_attribute("aria-modal") == "true"
    labelled = ov.get_attribute("aria-labelledby")
    assert labelled and page.locator(f"#{labelled}").inner_text() == "Publish?"


def test_tab_stays_inside_the_dialog(page):
    _fresh(page)
    page.focus("#ok")
    page.keyboard.press("Tab")
    assert active(page) == "modal-close"                          # wrapped to the first
    page.keyboard.press("Shift+Tab")
    assert active(page) == "ok"                                   # and back to the last


def test_escape_closes_it_and_focus_goes_back_to_the_opener(page):
    _fresh(page)
    page.keyboard.press("Escape")
    page.wait_for_timeout(120)
    assert page.locator(".modal-overlay").count() == 0            # Cancel was clicked
    assert active(page) == "opener"


def test_announce_writes_to_a_live_region(page):
    page.set_content("<!doctype html><html lang='en'><body></body></html>")
    page.add_script_tag(content=JS.read_text(encoding="utf-8"))
    page.evaluate("A11y.announce('Saved'); A11y.announce('Post failed', {assertive: true})")
    page.wait_for_timeout(100)
    assert page.locator("[role=status][aria-live=polite]").inner_text() == "Saved"
    assert page.locator("[role=alert][aria-live=assertive]").inner_text() == "Post failed"


def test_settings_toggles_and_status_dots_get_names(page):
    page.set_content("""<!doctype html><html lang='en'><body>
      <div class="settings-row"><div><span class="settings-label">Auto-sync settings</span></div>
        <label class="toggle-switch"><input type="checkbox" id="t1"><span></span></label></div>
      <span class="status-dot warn" id="d1"></span><span class="status-dot connected" id="d2"></span>
    </body></html>""")
    page.add_script_tag(content=JS.read_text(encoding="utf-8"))
    page.wait_for_timeout(50)
    lbl = page.get_attribute("#t1", "aria-labelledby")
    assert lbl and page.locator(f"#{lbl}").inner_text() == "Auto-sync settings"
    assert page.get_attribute("#d1", "aria-label") == "Needs attention"
    assert page.get_attribute("#d2", "aria-label") == "Working"
    page.evaluate("document.getElementById('d1').className = 'status-dot connected'")
    page.wait_for_timeout(50)
    assert page.get_attribute("#d1", "aria-label") == "Working"        # follows the state


def test_a_hidden_dialog_left_in_the_page_is_not_open(page):
    """4.45.2 (release review): dialogs that stay in the page hidden by a parent (publish check,
    the Ctrl+K palette) counted as open — Tab stopped working everywhere and Escape clicked a
    Cancel nobody could see."""
    page.set_content("""<!doctype html><html lang='en'><body>
      <button id="a">A</button><button id="b">B</button>
      <div class="publish-check-modal" style="display:none"><div role="dialog" class="publish-check-dialog">
        <h3>Publish check</h3><button id="hidden-cancel">Cancel</button></div></div>
      <script>document.getElementById('hidden-cancel').onclick = () => { window.clicked = true; };</script>
    </body></html>""")
    page.add_script_tag(content=JS.read_text(encoding="utf-8"))
    page.wait_for_timeout(50)
    assert page.evaluate("A11y._openDialogs().length") == 0
    page.focus("#a")
    page.keyboard.press("Tab")
    assert active(page) == "b"                                   # Tab still moves
    page.keyboard.press("Escape")
    page.wait_for_timeout(50)
    assert page.evaluate("window.clicked === true") is False     # nothing hidden was clicked
