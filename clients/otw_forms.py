"""OTW Archive forms (AO3, SquidgeWorld): pick the form a request posts to.

The archive uses per-form CSRF tokens: each form's ``authenticity_token`` is only valid for that form's own action.
A page carries several (the site-banner dismiss form, the skin switchers, the work form), so "the first token on the
page" is the wrong one whenever a banner is up, and AO3 answers the POST with ``/auth_error`` (4.78.1, AO3AUTHERR).

One forward pass with ``str.find`` (SECLOW4781): a regex over the whole page went quadratic on a page of unclosed
``<form`` tags (~50 s on 360 KB), and this runs inside the event loop.
"""
from __future__ import annotations

import re
from html import unescape

_ACTION_RE = re.compile(r'\saction="([^"]*)"', re.I)
_MAX_TAG = 4096          # a real <form ...> tag is a few hundred characters
# Site chrome that appears on every page: never the form a caller wants.
_CHROME_RE = re.compile(r"/users/[^/]+/end_banner|/skins/(?:\d+/set|unset)")


def _forms(page: str):
    """(start, end, action) of each complete ``<form>…</form>``, in order. Linear: every find moves forward."""
    low = page.lower()
    i = 0
    while True:
        s = low.find("<form", i)
        if s < 0:
            return
        e = low.find(">", s)
        end = low.find("</form>", e) if e >= 0 else -1
        if end < 0:
            return
        end += len("</form>")
        m = _ACTION_RE.search(page, s, e + 1) if e - s <= _MAX_TAG else None
        yield s, end, unescape(m.group(1)) if m else ""
        i = end


def otw_form(page: str, action: str) -> str:
    """The ``<form>`` on *page* whose action path fully matches the regex *action*, e.g. ``r"/works/\\d+"``.

    No match: the page without its chrome forms, so a token search still can't land on the banner's or a skin's.
    """
    chrome = []
    for s, end, act in _forms(page):
        path = re.sub(r"^https?://[^/]+", "", act.split("?", 1)[0].split("#", 1)[0]).rstrip("/") or "/"
        if re.fullmatch(action, path):
            return page[s:end]
        if _CHROME_RE.search(act):
            chrome.append((s, end))
    out, i = [], 0
    for s, end in chrome:
        out.append(page[i:s])
        i = end
    out.append(page[i:])
    return "".join(out)
