"""FurAffinity journal, filled in for you (spec 027, 4.62.2) — the desktop app only.

FA's journal form demands a CAPTCHA (live 2026-10-10), and PawPoller never gets past a bot check. So the desktop app
does everything up to it: it opens FA's real journal page in an app window, fills in the title, the FA-formatted
text and the rating, and leaves the CAPTCHA and the Post button to the person. When FA lands on the new journal
(``/journal/<id>/``), ``on_posted`` is told, so PawPoller can record the link.

The window shares the app's browser, so a FA sign-in made through Browser login (or in this window) carries over.
Signed out, FA shows its login page; once signed in, the window goes on to the journal form by itself.

Same threading rules as ``auth.browser_login``: only ``create_window`` here, never ``webview.start``.
"""
from __future__ import annotations

import json
import logging
import re
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

JOURNAL_PAGE = "https://www.furaffinity.net/controls/journal/"

# Fill the form that posts to /controls/journal/ (found by its action, as the server-side client does). Returns
# 'filled' or why not. The person still ticks the CAPTCHA and presses Post.
_FILL_JS = """(function (d) {
  var f = document.querySelector('form[action="/controls/journal/"]');
  if (!f) return 'noform';
  var s = f.querySelector('[name="subject"]'), m = f.querySelector('[name="message"]');
  if (!s || !m) return 'nofields';
  s.value = d.t; m.value = d.m;
  [s, m].forEach(function (el) { el.dispatchEvent(new Event('input', {bubbles: true})); });
  var r = f.querySelector('input[name="rating"][value="' + d.r + '"]');
  if (r) r.checked = true;
  m.scrollIntoView({block: 'center'});
  return 'filled';
})(%s)"""

_SIGNED_IN_JS = "!!document.querySelector('[action*=\"/logout\"], a[href*=\"/logout\"]')"


def fill_script(title: str, message: str, rating: str) -> str:
    """The script that fills the form — data passed as JSON, never pasted into the code."""
    return _FILL_JS % json.dumps({"t": (title or "")[:60], "m": message or "", "r": str(rating or "0")})


def on_fa(url: str) -> bool:
    """The page is FA's own (checked on the host, not anywhere in the address)."""
    host = (urlparse(url or "").hostname or "").lower()
    return host == "furaffinity.net" or host.endswith(".furaffinity.net")


def posted_journal(url: str) -> str:
    """The new journal's id when FA has landed on it, else ''."""
    if not on_fa(url):
        return ""
    m = re.match(r"/journal/(\d+)", urlparse(url).path or "")
    return m.group(1) if m else ""


def open_fa_journal(title: str, message: str, rating: str, on_posted=None) -> dict:
    """Open FA's journal page filled in. Returns at once ({"ok": True}); the window lives on its own."""
    try:
        import webview
    except ImportError:
        return {"ok": False, "message": "This only works in the PawPoller desktop app."}
    if not getattr(webview, "windows", None):
        return {"ok": False, "message": "This only works in the PawPoller desktop app."}
    win = webview.create_window("FurAffinity journal — PawPoller", JOURNAL_PAGE, width=1000, height=820)
    state = {"filled": False, "done": False}
    script = fill_script(title, message, rating)

    def _on_loaded():
        try:
            url = win.get_current_url() or ""
            jid = posted_journal(url)
            if jid and state["filled"] and not state["done"]:
                state["done"] = True
                if on_posted:
                    on_posted(jid, f"https://www.furaffinity.net/journal/{jid}/")
                return
            if state["filled"]:
                return
            if not on_fa(url):
                return                              # never fill or follow anything off FA
            if urlparse(url).path.startswith("/controls/journal"):
                state["filled"] = win.evaluate_js(script) == "filled"
            elif win.evaluate_js(_SIGNED_IN_JS):
                win.load_url(JOURNAL_PAGE)          # signed in now (e.g. after FA's login page): go to the form
        except Exception as e:                      # the window may be closing
            logger.debug("FA journal window: %s", e)

    win.events.loaded += _on_loaded
    return {"ok": True}
